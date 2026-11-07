from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from parity.replay import Replay


@pytest.mark.parametrize("scenario", ["payment_loss_restart", "concurrent_retry"])
@pytest.mark.parametrize("candidate_id", [True, 0, -1, "1"])
def test_recovery_and_concurrency_reject_invalid_legacy_ids(scenario, candidate_id):
    replay = Replay.__new__(Replay)
    replay.ids, replay.events = {}, []
    replay.successor, replay.worker = MagicMock(), MagicMock()
    replay.old, replay.new = MagicMock(), MagicMock()
    order = {"id": 1, "status": "paid", "amount": 1250, "items": []}
    replay.old.checkout.return_value = httpx.Response(201, json=order)
    actual = httpx.Response(201, json={**order, "id": candidate_id})
    replay.new.checkout.side_effect = (
        [httpx.Response(503, json={"error": "PENDING"}), actual]
        if scenario == "payment_loss_restart"
        else [actual] * 6
    )
    with pytest.raises(AssertionError, match="positive integer"):
        replay.run_scenario(scenario)


def test_transfer_checker_rejects_changed_id_on_retrieval(tmp_path):
    from parity.transfer_replay import evaluate

    reference = Path(__file__).resolve().parents[2] / "artifacts/reference/transfer/adapter.py"
    source = tmp_path / "unstable.py"
    source.write_text(
        reference.read_text()
        + """
original_handle = handle
def handle(ctx, method, path, body, query, headers):
    status, data = original_handle(ctx, method, path, body, query, headers)
    if method == "GET" and status == 200:
        data["id"] += 100
    return status, data
"""
    )
    report = evaluate(source, tmp_path / "trial")
    ordinary = next(row for row in report["scenarios"] if row["scenario"] == "ordinary")
    assert ordinary["status"] == "fail"
    assert "unstable ID correspondence" in ordinary["mismatch"]
