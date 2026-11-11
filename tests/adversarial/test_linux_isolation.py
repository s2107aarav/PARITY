import json
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI

from parity.sandbox import AdapterProcess
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
