import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from parity.sandbox import RESOURCE_POLICY, AdapterProcess, resident_bytes
from parity.serving import Server


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux namespace isolation")
def test_linux_worker_has_no_host_network_or_private_files(tmp_path):
    private = tmp_path / "private.txt"
    private.write_text("test canary")
    app = FastAPI()

    @app.get("/value")
    def value():
        return {"value": 7}

    source = tmp_path / "adapter.py"
    source.write_text(
        'def handle(ctx, method, path, body, query, headers):\n    with ctx.lock():\n        return 200, ctx.call("GET", "/value")\n'
    )
    server = Server(app)
    worker = None
    try:
        worker = AdapterProcess(source, server.url, tmp_path / "worker")
        with httpx.Client(trust_env=False) as client:
            assert client.get(worker.url + "/value").json() == {"value": 7}
        program = f"""import json, socket
result = {{}}
try:
    open({str(private)!r}).read()
    result['private'] = 'readable'
except OSError:
    result['private'] = 'denied'
try:
    socket.create_connection(('127.0.0.1', {server.port}), timeout=1)
    result['network'] = 'reachable'
except OSError:
    result['network'] = 'denied'
print(json.dumps(result))
"""
        command = worker.command()
        result = subprocess.run(
            [*command[:-2], "-c", program],
            cwd=worker.root,
            env={"PATH": "/usr/bin:/bin"},
            text=True,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"private": "denied", "network": "denied"}
        worker.kill()
        worker.restart()
        with httpx.Client(trust_env=False) as client:
            assert client.get(worker.url + "/value").json() == {"value": 7}
    finally:
        if worker:
            worker.close()
        server.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux namespace RSS isolation")
def test_linux_rss_monitor_tracks_workers_that_change_process_group(tmp_path, monkeypatch):
    source = tmp_path / "adapter.py"
    source.write_text("""import os
def handle(ctx, method, path, body, query, headers):
    os.setsid()
    return 200, {"ready": True}
""")
    # Exercise the OS boundary directly, independently of the AST capability filter.
    monkeypatch.setattr("parity.sandbox.check_source", lambda _: None)
    service = Server(FastAPI())
    worker = None
    try:
        worker = AdapterProcess(source, service.url, tmp_path / "worker")
        with httpx.Client(trust_env=False) as client:
            assert client.get(worker.url + "/detach").json() == {"ready": True}
        group_rss = 0
        for process in Path("/proc").glob("[0-9]*"):
            try:
                fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == worker.proc.pid:
                    for line in (process / "status").read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            group_rss += int(line.split()[1]) * 1024
            except (OSError, ValueError, IndexError):
                continue
        ceiling = group_rss + 1024 * 1024
        assert resident_bytes(worker.proc.pid) > ceiling
        monkeypatch.setitem(RESOURCE_POLICY, "rss_bytes", ceiling)
        worker.proc.wait(timeout=5)
        assert json.loads((worker.root / "termination.json").read_text())["reason"] == "rss"
    finally:
        if worker:
            worker.close()
        service.close()
