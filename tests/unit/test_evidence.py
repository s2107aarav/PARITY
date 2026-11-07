from pathlib import Path

import pytest

from parity.adversarial import calibrate
from parity.oneshot import Bundle, finalize
from parity.report import render

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "failure_kind, rejected", [("worker_infrastructure", 0), ("semantic_or_trial_error", 5)]
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
    result = calibrate(ROOT / "artifacts/reference/commerce/adapter.py", tmp_path)
    assert result["rejected"] == rejected and result["total"] == 5


def test_report_is_incomplete_when_reference_fails(tmp_path):
    artifact = ROOT / "artifacts/generated/commerce-live-002"
    evaluations = [
        {
            "role": role,
            "report": {
                "passed": 0 if role == "reference" else 1,
                "total": 1,
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


def test_single_shot_syntax_error_preserves_rejected_candidate(tmp_path):
    from parity.contracts import load

    artifact = ROOT / "artifacts/generated/commerce-live-002"
    source = "def handle(ctx, method, path, body, query, headers):\n    return (\n"
    bundle = Bundle(
        plan_json=(artifact / "plan.json").read_text(), source=source, explanation="test"
    )
    result = finalize(
        bundle,
        load(ROOT / "contracts/commerce/v1.json"),
        load(ROOT / "contracts/commerce/v2.json"),
        object(),
        tmp_path,
        0,
    )
    assert result["status"] == "generation_rejected"
    assert "SyntaxError" in result["validation_failure"]
    assert (tmp_path / "rejected-source.txt").read_text() == source
    assert not (tmp_path / "adapter.py").exists()
