import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


def test_wheel_installs_and_runs_outside_checkout(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "MANIFEST.in"):
        shutil.copyfile(repository / name, source / name)
    for name in ("apps", "parity"):
        shutil.copytree(
            repository / name, source / name, ignore=shutil.ignore_patterns("__pycache__")
        )
    cache = source / "parity/examples/artifacts/generated/commerce-live-002/__pycache__"
    cache.mkdir()
    (cache / "adapter.cpython-312.pyc").write_bytes(b"stale bytecode")
    build = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(tmp_path / "dist"), str(source)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert not any(
            "__pycache__" in name or name.endswith(".pyc") for name in archive.namelist()
        )
    with tarfile.open(next((tmp_path / "dist").glob("*.tar.gz"))) as archive:
        assert not any(
            "__pycache__" in name or name.endswith(".pyc") for name in archive.getnames()
        )
    installed = tmp_path / "installed"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    outside = tmp_path / "user-project"
    outside.mkdir()
    program = """import sys
sys.path.insert(0, sys.argv.pop(1))
from parity.cli import main, artifact_path
from parity.resources import examples
assert artifact_path("commerce-live-002").is_relative_to(examples())
main()
"""
    command = [sys.executable, "-c", program, str(installed)]
    init = subprocess.run(
        [*command, "init"], cwd=outside, capture_output=True, text=True, timeout=10
    )
    assert init.returncode == 0, init.stdout + init.stderr
    assert json.loads((outside / "parity.json").read_text())["allow_writes"] is False
    replay = subprocess.run(
        [*command, "validate", "--adapter", "transfer-live-001", "--suite", "transfer"],
        cwd=outside,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert "6/6 passed" in replay.stdout
    assert (outside / ".parity/reports").is_dir()
    assert not (installed / "artifacts/reports").exists()
