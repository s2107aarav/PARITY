import json
from pathlib import Path

import pytest

from parity.cli import artifact_path
from parity.replay import SCENARIOS, evaluate
from parity.resources import examples
from parity.transfer_replay import evaluate as evaluate_transfer

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def generated_report(tmp_path_factory):
    return evaluate(
        artifact_path("commerce-live-002") / "adapter.py", tmp_path_factory.mktemp("generated")
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_pinned_generated_workflows(generated_report, scenario):
    result = next(x for x in generated_report["scenarios"] if x["scenario"] == scenario)
    assert result["status"] == "pass", json.dumps(result, indent=2)


def test_pinned_generated_transfer(tmp_path):
    report = evaluate_transfer(artifact_path("transfer-live-001") / "adapter.py", tmp_path)
    assert report["passed"] == report["total"], report


def test_pins_are_exact_and_old_failure_is_preserved():
    from parity.sandbox import digest

    pins = json.loads((Path(__file__).resolve().parents[2] / "artifacts/pins.json").read_text())
    for relative, expected in pins["files"].items():
        assert digest(Path(__file__).resolve().parents[2] / relative) == expected, (
            f"immutable evidence changed: {relative}"
        )
    first = examples() / "artifacts/generated/commerce-live-002/public-v1/public-result.json"
    revised = examples() / "artifacts/generated/commerce-live-002/public-v2/public-result.json"
    assert json.loads(first.read_text())["passed"] is False
    assert json.loads(revised.read_text())["passed"] is True
