import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from hypothesis.strategies import integers

from apps.commerce_v1 import create_app
from parity.replay import effect


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(str(Path(self.temp.name) / "v1.db"))
        self.client = TestClient(self.app)
        self.orders = {}

    @rule(key=integers(min_value=0, max_value=8))
    def checkout_or_retry(self, key):
        response = self.client.post(
            "/checkout",
            json={
                "items": [{"sku": "COFFEE", "quantity": 1}],
                "amount": 1250,
                "payment_token": "ok",
            },
            headers={"Idempotency-Key": str(key)},
        )
        assert response.status_code == 201
        row = response.json()
        if key in self.orders:
            assert self.orders[key] == row["id"]
        self.orders[key] = row["id"]

    @rule(key=integers(min_value=0, max_value=8))
    def cancel(self, key):
        if key in self.orders:
            assert self.client.post(f"/orders/{self.orders[key]}/cancel").status_code == 200

    @invariant()
    def ledger_and_inventory(self):
        effect(self.app.state.store.snapshot(), 1)

    def teardown(self):
        self.client.close()
        self.app.state.store.close()
        self.temp.cleanup()


TestLedger = LedgerMachine.TestCase
TestLedger.settings = settings(
    max_examples=12, stateful_step_count=25, deadline=None, derandomize=True
)


class AdapterLedgerMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        from parity.replay import Replay
        from parity.resources import examples

        self.temp = tempfile.TemporaryDirectory()
        self.replay = Replay(
            examples() / "artifacts/generated/commerce-live-002/adapter.py", Path(self.temp.name)
        )
        self.orders = {}

    @rule(key=integers(min_value=0, max_value=4))
    def checkout_or_retry(self, key):
        old, new = self.replay.checkout(key=str(key))
        self.orders[key] = (old["id"], new["id"])

    @rule(key=integers(min_value=0, max_value=4))
    def cancel(self, key):
        if key in self.orders:
            old, new = self.orders[key]
            self.replay.pair(
                "cancel", lambda: self.replay.old.cancel(old), lambda: self.replay.new.cancel(new)
            )

    @invariant()
    def effects_match(self):
        self.replay.verify_state()

    def teardown(self):
        self.replay.close()
        self.temp.cleanup()


TestAdapterLedger = AdapterLedgerMachine.TestCase
TestAdapterLedger.settings = settings(
    max_examples=4, stateful_step_count=12, deadline=None, derandomize=True
)
