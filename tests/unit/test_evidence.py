import subprocess
import sys
import zipfile

import pytest

from parity.adversarial import calibrate
from parity.oneshot import Bundle, finalize
from parity.report import render
from parity.resources import examples
from parity.sandbox import digest
from scripts.package_release import build_release


@pytest.mark.parametrize(
    "failure_kind, rejected",
    [("worker_infrastructure", 0), ("trial_error", 0), ("semantic_mismatch", 5)],
)
def test_calibration_does_not_count_infrastructure_failure(
    tmp_path, monkeypatch, failure_kind, rejected
):
    monkeypatch.setattr(
        "parity.adversarial.evaluate",
        lambda *args, **kwargs: {
            "passed": 0,
            "scenarios": [{"failure_kind": failure_kind, "mismatch": "test failure"}],
        },
    )
    result = calibrate(examples() / "artifacts/reference/commerce/adapter.py", tmp_path)
    assert result["rejected"] == rejected and result["total"] == 5


def test_report_is_incomplete_when_reference_fails(tmp_path):
    artifact = examples() / "artifacts/generated/commerce-live-002"
    evaluations = [
        {
            "role": role,
            "report": {
                "passed": 0 if role == "reference" else 1,
                "total": 1,
                "artifact_sha256": digest(artifact / "adapter.py"),
                "checker_sha256": "checker",
                "legacy_client_sha256": "client",
            },
        }
        for role in ("parity", "reference", "transfer")
    ]
    report = render(
        tmp_path, evaluations, artifact, {}, {"rejected": 5, "total": 5}, transfer_provenance="test"
    )
    assert report["summary"]["status"].startswith("INCOMPLETE")


def test_report_refuses_source_that_differs_from_evaluation(tmp_path):
    artifact = examples() / "artifacts/generated/commerce-live-002"
    evaluations = [
        {"role": role, "report": {"artifact_sha256": "wrong"}} for role in ("parity", "transfer")
    ]
    with pytest.raises(ValueError, match="does not match evaluated adapter"):
        render(tmp_path, evaluations, artifact, {}, {}, transfer_provenance="test")
    assert not (tmp_path / "report-data.json").exists()


def test_optimized_python_refuses_to_run_acceptance(tmp_path):
    output = tmp_path / "evaluation"
    result = subprocess.run(
        [
            sys.executable,
            "-O",
            "-m",
            "parity.cli",
            "validate",
            "--adapter",
            "commerce-live-002",
            "--out",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert "requires Python assertions" in result.stdout
    assert not output.exists()


def test_release_excludes_untracked_data_and_preserves_previous_archive_on_failure(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    (tmp_path / "README.md").write_text("Tracked release text\n")
    (tmp_path / "private.json").write_text('{"example": "untracked run output"}')
    git("add", "README.md")
    output = tmp_path / "release.zip"
    build_release(tmp_path, output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"PARITY/README.md", "PARITY/REDACTIONS.json"}
    original = output.read_bytes()
    (tmp_path / "linked.txt").symlink_to(tmp_path / "private.json")
    git("add", "linked.txt")
    with pytest.raises(ValueError, match="release input must be a regular file"):
        build_release(tmp_path, output)
    assert output.read_bytes() == original


def test_single_shot_syntax_error_preserves_rejected_candidate(tmp_path):
    from parity.contracts import load

    artifact = examples() / "artifacts/generated/commerce-live-002"
    source = "def handle(ctx, method, path, body, query, headers):\n    return (\n"
    bundle = Bundle(
        plan_json=(artifact / "plan.json").read_text(), source=source, explanation="test"
    )
    result = finalize(
        bundle,
        load(examples() / "contracts/commerce/v1.json"),
        load(examples() / "contracts/commerce/v2.json"),
        object(),
        tmp_path,
        0,
    )
    assert result["status"] == "generation_rejected"
    assert "SyntaxError" in result["validation_failure"]
    assert (tmp_path / "rejected-source.txt").read_text() == source
    assert not (tmp_path / "adapter.py").exists()
