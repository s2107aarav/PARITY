import json
from pathlib import Path

import pytest

from parity.replay import SCENARIOS, Replay
from parity.transfer_replay import evaluate as transfer_evaluate

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_reference_workflows(reference_report, scenario):
    result = next(x for x in reference_report["scenarios"] if x["scenario"] == scenario)
    assert result["status"] == "pass", json.dumps(result, indent=2)


@pytest.mark.parametrize("point", ["reserve.after", "payment.before", "confirm.before"])
def test_additional_ambiguous_steps(tmp_path, point):
    replay = Replay(ROOT / "artifacts/reference/commerce/adapter.py", tmp_path)
    try:
        replay.successor.app.state.faults.arm(point)
        a = replay.old.checkout()
        assert replay.new.checkout().status_code == 503
        replay.worker.restart()
        b = replay.new.checkout()
        assert a.status_code == b.status_code == 201
        replay.verify_state()
    finally:
        replay.close()


def test_duplicate_sku_and_restart_identity(tmp_path):
    replay = Replay(ROOT / "artifacts/reference/commerce/adapter.py", tmp_path)
    try:
        a, b = replay.checkout(
            items=[{"sku": "TEA", "quantity": 2}, {"sku": "TEA", "quantity": 3}], amount=3750
        )
        replay.worker.restart()
        replay.pair(
            "persisted identity",
            lambda: replay.old.order(a["id"]),
            lambda: replay.new.order(b["id"]),
        )
        replay.verify_state()
    finally:
        replay.close()


def test_transfer_reference(tmp_path):
    report = transfer_evaluate(ROOT / "artifacts/reference/transfer/adapter.py", tmp_path)
    assert report["passed"] == report["total"], report
