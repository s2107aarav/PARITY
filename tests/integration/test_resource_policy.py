import json
from pathlib import Path

import pytest

from apps.commerce_v2 import create_app
from parity.replay import evaluate
from parity.resources import examples
from parity.sandbox import RESOURCE_POLICY, AdapterProcess, WorkerFailure
from parity.serving import Server


@pytest.mark.parametrize("repeat", range(3))
def test_sandboxed_concurrency_and_recovery(tmp_path, repeat):
    report = evaluate(
        examples() / "artifacts/generated/commerce-live-002/adapter.py",
        tmp_path,
        scenarios=("concurrent_retry", "payment_loss_restart"),
    )
    assert report["passed"] == report["total"] == 2, report


def test_worker_limit_failure_is_not_business_success(tmp_path, monkeypatch):
    server = Server(create_app(str(tmp_path / "service.db")))
    worker = AdapterProcess(
        examples() / "artifacts/reference/commerce/adapter.py", server.url, tmp_path / "worker"
    )
    try:
        wall_seconds = RESOURCE_POLICY["wall_seconds"]
        monkeypatch.setitem(RESOURCE_POLICY, "wall_seconds", 0)
        worker.proc.wait(timeout=5)
        with pytest.raises(WorkerFailure, match="wall_time"):
            worker.ensure_alive()
        assert json.loads((worker.root / "termination.json").read_text())["reason"] == "wall_time"
        monkeypatch.setitem(RESOURCE_POLICY, "wall_seconds", wall_seconds)
        worker.restart()
        worker.ensure_alive()
        assert not (worker.root / "termination.json").exists()
    finally:
        worker.close()
        server.close()


def test_no_sandbox_fails_closed(tmp_path, monkeypatch):
    import parity.sandbox as sandbox

    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Refusing untrusted"):
        AdapterProcess(
            examples() / "artifacts/reference/commerce/adapter.py", "http://127.0.0.1:1", tmp_path
        )
    assert not (tmp_path / "worker.log").exists()


def test_spawn_failure_closes_worker_log(tmp_path, monkeypatch):
    import parity.sandbox as sandbox

    opened = []
    original_open = Path.open

    def capture_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.name == "worker.log":
            opened.append(stream)
        return stream

    def failed_spawn(*args, **kwargs):
        raise OSError("test spawn failure")

    monkeypatch.setattr(Path, "open", capture_open)
    monkeypatch.setattr(AdapterProcess, "command", lambda self: ["test-worker"])
    monkeypatch.setattr(sandbox.subprocess, "Popen", failed_spawn)
    with pytest.raises(WorkerFailure, match="test spawn failure"):
        AdapterProcess(
            examples() / "artifacts/reference/commerce/adapter.py", "http://127.0.0.1:1", tmp_path
        )
    assert len(opened) == 1 and opened[0].closed


def test_linux_rss_includes_descendants_after_process_group_changes(tmp_path, monkeypatch):
    import parity.sandbox as sandbox

    proc = tmp_path / "proc"
    for pid, parent, group, rss in (
        (100, 1, 100, 1),
        (101, 100, 101, 2),
        (102, 101, 102, 3),
        (103, 1, 100, 4),
        (999, 1, 999, 100),
    ):
        process = proc / str(pid)
        process.mkdir(parents=True)
        (process / "stat").write_text(f"{pid} (worker with spaces) S {parent} {group} 0\n")
        (process / "status").write_text(f"VmRSS:\t{rss} kB\n")
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "Path", lambda value: proc if value == "/proc" else Path(value))
    assert sandbox.resident_bytes(100) == 10 * 1024
    with pytest.raises(RuntimeError, match="RSS unavailable"):
        sandbox.resident_bytes(500)
