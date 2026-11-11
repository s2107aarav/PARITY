import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from parity.replay import SCENARIOS, Replay
from parity.resources import examples
from parity.transfer_replay import evaluate as transfer_evaluate


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_reference_workflows(reference_report, scenario):
    result = next(x for x in reference_report["scenarios"] if x["scenario"] == scenario)
    assert result["status"] == "pass", json.dumps(result, indent=2)


@pytest.mark.parametrize("point", ["reserve.after", "payment.before", "confirm.before"])
def test_additional_ambiguous_steps(tmp_path, point):
    replay = Replay(examples() / "artifacts/reference/commerce/adapter.py", tmp_path)
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
    replay = Replay(examples() / "artifacts/reference/commerce/adapter.py", tmp_path)
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
    report = transfer_evaluate(examples() / "artifacts/reference/transfer/adapter.py", tmp_path)
    assert report["passed"] == report["total"], report


@pytest.mark.parametrize("point", ["reserve.after", "payment.after", "confirm.after"])
def test_hard_kill_after_commit_before_checkpoint(tmp_path, point):
    replay = Replay(examples() / "artifacts/generated/commerce-live-002/adapter.py", tmp_path)
    committed, release = threading.Event(), threading.Event()
    original = replay.successor.app.state.faults.hit

    def pause(name):
        if name == point and not committed.is_set():
            committed.set()
            assert release.wait(timeout=10), "test did not release committed request"
        original(name)

    replay.successor.app.state.faults.hit = pause
    try:
        expected = replay.old.checkout(key="interrupted")
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(replay.new.checkout, key="interrupted")
            try:
                assert committed.wait(timeout=5), "upstream effect did not commit"
                replay.worker.kill()
            finally:
                release.set()
            try:
                assert request.result(timeout=5).status_code >= 500
            except httpx.HTTPError:
                pass
        replay.worker.restart()
        recovered = replay.new.checkout(key="interrupted")
        assert expected.status_code == recovered.status_code == 201
        replay.verify_state()
    finally:
        release.set()
        replay.close()
