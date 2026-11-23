"""Fail-closed execution for generated modules. AST filtering is defense in depth only."""

import ast
import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class AdapterSource:
    path: Path
    content: bytes

    @classmethod
    def read(cls, source: "AdapterSource | Path | str") -> "AdapterSource":
        if isinstance(source, AdapterSource):
            return source
        path = Path(source).resolve()
        return cls(path, path.read_bytes())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def check_source(source: str):
    tree = ast.parse(source)
    forbidden = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
        "help",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [node.module] if isinstance(node, ast.ImportFrom) else [x.name for x in node.names]
            )
            if any(
                x not in {"parity.runtime", "urllib.parse", "json", "hashlib", "re", "math"}
                for x in modules
            ):
                raise ValueError("plugin import outside allowed surface")
            if isinstance(node, ast.ImportFrom) and node.level:
                raise ValueError("plugin relative imports are not allowed")
            if "parity.runtime" in modules and (
                not isinstance(node, ast.ImportFrom)
                or any(alias.name != "UpstreamError" for alias in node.names)
            ):
                raise ValueError("plugin may only import UpstreamError from parity.runtime")
            if "urllib.parse" in modules and (
                not isinstance(node, ast.ImportFrom)
                or any(alias.name not in {"quote", "unquote"} for alias in node.names)
            ):
                raise ValueError("plugin may only import quote and unquote from urllib.parse")
        if isinstance(node, ast.Name) and (node.id in forbidden or node.id.startswith("__")):
            raise ValueError("plugin uses forbidden builtin")
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith("_")
            or node.attr
            in {
                "store",
                "http",
                "engine",
                "connection",
                "base",
                "trace",
                "local",
                "thread_lock",
                "request_scope",
                "record_request",
                "record_result",
                "import_ids",
                "close",
            }
        ):
            raise ValueError("plugin accesses private capability")
    if not any(isinstance(x, ast.FunctionDef) and x.name == "handle" for x in tree.body):
        raise ValueError("adapter must expose handle(ctx, method, path, body, query, headers)")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


RESOURCE_POLICY = {
    "cpu_seconds": 25,
    "wall_seconds": 120,
    "descriptors": 128,
    "file_bytes": 32 * 1024 * 1024,
    "rss_bytes": 768 * 1024 * 1024,
    "linux_address_space_bytes": 2 * 1024 * 1024 * 1024,
    "worker_threads": 8,
    "rss_poll_seconds": 0.25,
}


class WorkerFailure(RuntimeError):
    """Infrastructure/resource failure, distinct from semantic incompatibility."""


def resident_bytes(pid: int) -> int:
    if sys.platform.startswith("linux"):
        # Include descendants even if they create a new session/process group.
        # PID namespace reparenting keeps detached workers under bubblewrap's subtree.
        children: dict[int, list[int]] = {}
        samples: dict[int, int] = {}
        members = {pid}
        for process in Path("/proc").glob("[0-9]*"):
            try:
                process_id = int(process.name)
                fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
                children.setdefault(int(fields[1]), []).append(process_id)
                if int(fields[2]) == pid:
                    members.add(process_id)
                for line in (process / "status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        samples[process_id] = int(line.split()[1]) * 1024
            except (OSError, ValueError, IndexError):
                continue  # Processes may exit during enumeration.
        pending = list(members)
        visited = set()
        while pending:
            process_id = pending.pop()
            if process_id in visited:
                continue
            visited.add(process_id)
            pending.extend(children.get(process_id, []))
        total = sum(samples.get(process_id, 0) for process_id in visited)
        if total:
            return total
        raise RuntimeError("worker process RSS unavailable from procfs")
    sample = subprocess.run(
        ["/bin/ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    return int(sample.stdout.strip()) * 1024


class AdapterProcess:
    def __init__(
        self,
        source: Path | AdapterSource,
        upstream: str,
        root: Path,
        *,
        trusted=False,
        trace_bodies=True,
    ):
        source_bytes = AdapterSource.read(source).content
        check_source(source_bytes.decode("utf-8"))
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.resources = ExitStack()
        self.closed = False
        self.sockets: Path | None = None
        self.trusted = trusted
        self.proc: subprocess.Popen | None = None
        self.log: TextIO | None = None
        self.monitor_stop = threading.Event()
        self.monitor_thread: threading.Thread | None = None
        try:
            lease = self.resources.enter_context((self.root / "worker.lock").open("a"))
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkerFailure("Adapter runtime is already in use") from exc
            self._prepare(source_bytes, upstream, trace_bodies)
            self._start()
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise WorkerFailure(f"Adapter could not start: {exc}") from exc
        except BaseException:
            self.close()
            raise

    def _prepare(self, source_bytes, upstream, trace_bodies):
        (self.root / "state").mkdir(exist_ok=True)
        package = self.root / "parity"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("")
        for name in ("http_values.py", "runtime.py", "storage.py", "worker.py"):
            shutil.copyfile(Path(__file__).parent / name, package / name)
        (self.root / "adapter.py").write_bytes(source_bytes)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        config = {
            "upstream": upstream,
            "port": self.port,
            "trace_bodies": trace_bodies,
            "limits": RESOURCE_POLICY,
        }
        if sys.platform.startswith("linux") and shutil.which("bwrap"):
            from parity.relay import relay_app
            from parity.serving import Server

            self.sockets = Path(
                self.resources.enter_context(
                    tempfile.TemporaryDirectory(prefix="parity-sockets-", dir="/tmp")
                )
            )
            config["upstream_socket"] = str(self.sockets / "upstream")
            config["listen_socket"] = str(self.sockets / "worker")
            upstream_relay = Server(relay_app(upstream), socket_path=config["upstream_socket"])
            self.resources.callback(upstream_relay.close)
            client_relay = Server(relay_app("http://worker", socket_path=config["listen_socket"]))
            self.resources.callback(client_relay.close)
            self.url = client_relay.url
        (self.root / "config.json").write_text(json.dumps(config))

    def command(self):
        cmd = [sys.executable, "-s", "-B", "-m", "parity.worker"]
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            # Do not grant the repository, HOME, checker or reference fixture read access.
            # Python framework and venv packages are explicit read-only roots.
            roots = {
                str(Path(sys.base_prefix).resolve()),
                str(Path(sys.prefix).resolve()),
                str(self.root),
            }
            ancestors = {str(parent) for root in roots for parent in Path(root).parents}
            reads = " ".join(
                [
                    *(f"(subpath {json.dumps(x)})" for x in sorted(roots)),
                    *(f"(literal {json.dumps(x)})" for x in sorted(ancestors)),
                ]
            )
            state = json.dumps(str(self.root / "state"))
            host_port = int(
                json.loads((self.root / "config.json").read_text())["upstream"].rsplit(":", 1)[1]
            )
            profile = f"""(version 1)
(deny default)
(allow process-exec (subpath {json.dumps(str(Path(sys.base_prefix).resolve()))}))
(allow sysctl-read mach-lookup)
(allow file-read* {reads} (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share") (subpath "/Library/Apple") (literal "/dev/urandom") (literal "/dev/null") (literal "/dev/random") (literal "/private/etc/localtime"))
(allow file-write* (subpath {state}) (literal "/dev/null"))
(allow network-bind (local ip "localhost:{self.port}"))
(allow network-inbound (local ip "localhost:{self.port}"))
(allow network-outbound (remote ip "localhost:{host_port}"))
"""
            (self.root / "sandbox.sb").write_text(profile)
            self.isolation = "macOS Seatbelt; explicit read roots; loopback upstream only"
            return ["sandbox-exec", "-f", str(self.root / "sandbox.sb"), *cmd]
        if sys.platform.startswith("linux") and shutil.which("bwrap"):
            # Library mounts only; evaluator and artifacts directories are not mounted.
            args = [
                "bwrap",
                "--die-with-parent",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--unshare-net",
                "--ro-bind",
                "/usr",
                "/usr",
                "--ro-bind",
                "/lib",
                "/lib",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
            ]
            for path in (
                "/lib64",
                str(Path(sys.prefix).resolve()),
                str(Path(sys.base_prefix).resolve()),
            ):
                if Path(path).exists() and not path.startswith("/usr/"):
                    args += ["--ro-bind", path, path]
            root, state = str(self.root), str(self.root / "state")
            args += ["--ro-bind", root, root, "--bind", state, state, "--chdir", root]
            if self.sockets:
                args += ["--bind", str(self.sockets), str(self.sockets)]
            self.isolation = "bubblewrap filesystem/pid/network isolation; Unix-socket HTTP relay"
            return [*args, *cmd]
        if not self.trusted:
            raise WorkerFailure(
                "No supported OS sandbox. Refusing untrusted code. Use macOS Seatbelt or Linux bubblewrap."
            )
        self.isolation = "trusted reference only; subprocess resource limits"
        return cmd

    def _start(self):
        self.monitor_stop.clear()
        (self.root / "termination.json").unlink(missing_ok=True)
        command = self.command()
        self.log = (self.root / "worker.log").open("a")
        self.proc = subprocess.Popen(
            command,
            cwd=self.root,
            env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        proc = self.proc
        (self.root / "resources.json").write_text(
            json.dumps(
                {
                    "policy": RESOURCE_POLICY,
                    "platform": platform.system(),
                    "machine": platform.machine(),
                    "python": platform.python_version(),
                    "isolation": self.isolation,
                    "address_space_enforced": sys.platform.startswith("linux"),
                    "rss_control": "sampled soft bound; transient overshoot possible",
                },
                indent=2,
            )
        )

        def monitor():
            started = time.monotonic()
            while (
                not self.monitor_stop.wait(RESOURCE_POLICY["rss_poll_seconds"])
                and proc.poll() is None
            ):
                reason = (
                    "wall_time"
                    if time.monotonic() - started > RESOURCE_POLICY["wall_seconds"]
                    else None
                )
                try:
                    if resident_bytes(proc.pid) > RESOURCE_POLICY["rss_bytes"]:
                        reason = "rss"
                except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
                    if proc.poll() is not None:
                        return
                    reason = "resource_monitor_unavailable"
                if reason:
                    try:
                        (self.root / "termination.json").write_text(json.dumps({"reason": reason}))
                    finally:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    return

        self.monitor_thread = threading.Thread(target=monitor, daemon=True)
        self.monitor_thread.start()
        deadline = time.monotonic() + 12
        with httpx.Client(trust_env=False, timeout=0.3) as client:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                try:
                    if client.get(self.url + "/__parity_health").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        raise WorkerFailure(
            f"Adapter did not start (exit={self.proc.returncode}): "
            + (self.root / "worker.log").read_text(errors="replace")[-5000:]
        )

    def ensure_alive(self):
        if self.proc is None or self.proc.poll() is not None:
            detail = self.root / "termination.json"
            reason = (
                detail.read_text()
                if detail.exists()
                else "exit/signal; inspect worker.log (CPU/AS limits may apply)"
            )
            raise WorkerFailure(f"worker infrastructure/resource failure: {reason}")

    def restart(self):
        if self.closed:
            raise WorkerFailure("Adapter runtime has been closed")
        try:
            self._stop_worker()
            if self.sockets:
                (self.sockets / "worker").unlink(missing_ok=True)
            self._start()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._stop_worker()
        finally:
            self.resources.close()

    def _stop_worker(self):
        self.monitor_stop.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=3)
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait()
        elif self.proc:
            self.proc.wait()
        if self.log:
            self.log.close()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
