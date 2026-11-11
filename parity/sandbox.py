"""Fail-closed execution for generated modules. AST filtering is defense in depth only."""

import ast
import hashlib
import json
import os
import platform
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, TextIO

import httpx


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
            if any(x not in {"parity.runtime", "json", "hashlib", "re", "math"} for x in modules):
                raise ValueError("plugin import outside allowed surface")
            if isinstance(node, ast.ImportFrom) and node.level:
                raise ValueError("plugin relative imports are not allowed")
            if "parity.runtime" in modules and (
                not isinstance(node, ast.ImportFrom)
                or any(alias.name != "UpstreamError" for alias in node.names)
            ):
                raise ValueError("plugin may only import UpstreamError from parity.runtime")
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


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    if sys.platform.startswith("linux"):
        # AS includes interpreter mappings and reserved pthread stacks, not just live memory.
        ceiling = RESOURCE_POLICY["linux_address_space_bytes"]
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))


def resident_bytes(pid: int) -> int:
    if sys.platform.startswith("linux"):
        # bubblewrap can remain a supervisor: measure the whole inherited process group,
        # not just its small launcher. No --new-session is used inside this group.
        total = 0
        for process in Path("/proc").glob("[0-9]*"):
            try:
                fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) != pid:
                    continue
                for line in (process / "status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1]) * 1024
            except (OSError, ValueError, IndexError):
                continue  # Processes may exit during enumeration.
        if total:
            return total
        raise RuntimeError("worker process-group RSS unavailable from procfs")
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
        self, source: Path, upstream: str, root: Path, *, trusted=False, trace_bodies=True
    ):
        check_source(source.read_text())
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "state").mkdir(exist_ok=True)
        package = self.root / "parity"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("")
        for name in ("runtime.py", "storage.py", "worker.py"):
            shutil.copyfile(Path(__file__).parent / name, package / name)
        shutil.copyfile(source, self.root / "adapter.py")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.relays: list[Any] = []
        self.sockets = None
        config = {"upstream": upstream, "port": self.port, "trace_bodies": trace_bodies}
        if sys.platform.startswith("linux") and shutil.which("bwrap"):
            from parity.relay import relay_app
            from parity.serving import Server

            self.sockets = tempfile.TemporaryDirectory(prefix="parity-sockets-", dir="/tmp")
            socket_root = Path(self.sockets.name)
            config["upstream_socket"] = str(socket_root / "upstream")
            config["listen_socket"] = str(socket_root / "worker")
            try:
                self.relays.append(
                    Server(relay_app(upstream), socket_path=config["upstream_socket"])
                )
                self.relays.append(
                    Server(relay_app("http://worker", socket_path=config["listen_socket"]))
                )
                self.url = self.relays[-1].url
            except BaseException:
                for relay in self.relays:
                    relay.close()
                self.sockets.cleanup()
                raise
        (self.root / "config.json").write_text(json.dumps(config))
        self.trusted = trusted
        self.proc: subprocess.Popen | None = None
        self.log: TextIO | None = None
        self.monitor_stop = threading.Event()
        self.monitor_thread: threading.Thread | None = None
        try:
            self.start()
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise WorkerFailure(f"Adapter could not start: {exc}") from exc
        except BaseException:
            self.close()
            raise

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
        if shutil.which("bwrap"):
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
            args += ["--bind", str(self.root), str(self.root), "--chdir", str(self.root)]
            if self.sockets:
                args += ["--bind", self.sockets.name, self.sockets.name]
            self.isolation = "bubblewrap filesystem/pid/network isolation; Unix-socket HTTP relay"
            return [*args, *cmd]
        if not self.trusted:
            raise WorkerFailure(
                "No supported OS sandbox. Refusing untrusted code. Use macOS Seatbelt or Linux bubblewrap."
            )
        self.isolation = "trusted reference only; subprocess resource limits"
        return cmd

    def start(self):
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
            preexec_fn=_limits,
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
            while not self.monitor_stop.wait(0.25) and proc.poll() is None:
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
        self.close()
        raise WorkerFailure(
            f"Adapter did not start (exit={self.proc.returncode}): "
            + (self.root / "worker.log").read_text()[-5000:]
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
        self.close(keep_relays=True)
        config = json.loads((self.root / "config.json").read_text())
        if config.get("listen_socket"):
            Path(config["listen_socket"]).unlink(missing_ok=True)
        try:
            self.start()
        except BaseException:
            self.close()
            raise

    def close(self, *, keep_relays=False):
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
        if not keep_relays:
            for relay in self.relays:
                relay.close()
            self.relays.clear()
            if self.sockets:
                self.sockets.cleanup()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
