import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.commerce_v2 import create_app
from parity.sandbox import AdapterProcess
from parity.serving import Server

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt boundary proof")
def test_real_os_boundary_denies_checker_reference_and_home(tmp_path):
    credential = tmp_path / "home" / ".codex" / "auth.json"
    credential.parent.mkdir(parents=True)
    credential.write_text('{"token": "test-only-canary"}')
    assert credential.read_text() == '{"token": "test-only-canary"}'
    service = Server(create_app(str(tmp_path / "v2.db")))
    worker = AdapterProcess(
        ROOT / "artifacts/reference/commerce/adapter.py", service.url, tmp_path / "worker"
    )
    try:
        protected = [
            ROOT / "parity/replay.py",
            ROOT / "artifacts/reference/commerce/adapter.py",
            credential,
        ]
        # Harmless trusted canary program uses the EXACT worker OS policy, outside AST filtering.
        code = "import json, os\nresults=[]\n"
        for path in protected:
            code += f"try:\n open({str(path)!r}).read(1)\n results.append('READABLE')\nexcept PermissionError:\n results.append('DENIED')\n"
        code += "print(json.dumps({'files':results,'secret_present':'PARITY_TEST_SECRET' in os.environ}))"
        env = {"PATH": "/usr/bin:/bin"}
        assert "PARITY_TEST_SECRET" not in env
        result = subprocess.run(
            ["sandbox-exec", "-f", str(worker.root / "sandbox.sb"), sys.executable, "-c", code],
            cwd=worker.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"files": ["DENIED"] * 3, "secret_present": False}
    finally:
        worker.close()
        service.close()
