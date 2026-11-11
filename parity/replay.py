"""Externally owned evaluator. No model or candidate may redefine acceptance predicates.

This file is never mounted in the generated worker. SHA256 guards detect accidental mutation.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apps.commerce_v1 import create_app as v1_app
from apps.commerce_v2 import create_app as v2_app
from apps.common import CATALOG
from apps.legacy_client import LegacyClient
from parity.comparison import compare_json, validate_response
from parity.contracts import load
from parity.resources import examples
from parity.sandbox import AdapterProcess, WorkerFailure, digest
from parity.serving import Server

SCENARIOS = (
    "ordinary",
    "errors",
    "lost_response",
    "payment_loss_restart",
    "reserve_failure",
    "confirmation_loss",
    "cancellation",
    "pagination",
    "concurrent_retry",
    "key_conflict",
    "compensation_retry",
    "compensation_loss_restart",
    "cancel_loss_restart",
    "rejected_key_reuse",
    "error_precedence",
)


def effect(snapshot, version):
    orders = list(snapshot.get("orders", {}).values())
    ledger = list(snapshot.get("payments", {}).values())
    normalized = []
    for row in orders:
        if version == 1:
            items = row["items"]
            status, amount, oid = row["status"], row["amount"], row["id"]
        else:
            oid = row["order_ref"]
            charged = any(x["order_ref"] == oid and x["kind"] == "charge" for x in ledger)
            if row["phase"] == "cancelled" and not charged:
                continue  # Internal compensated draft: not an observable legacy order.
            if row["phase"] != "confirmed" and row["phase"] != "cancelled":
                raise AssertionError(f"unsettled V2 draft {row['phase']}")
            status = "paid" if row["phase"] == "confirmed" else "cancelled"
            amount = row["total_minor"]
            items = [{"sku": x["product_code"], "quantity": x["units"]} for x in row["lines"]]
        charge = [
            x
            for x in ledger
            if x.get("order_id", x.get("order_ref")) == oid and x["kind"] == "charge"
        ]
        refunds = [
            x
            for x in ledger
            if x.get("order_id", x.get("order_ref")) == oid and x["kind"] == "refund"
        ]
        assert len(charge) == 1 and charge[0]["amount"] == amount, "charge count/amount mismatch"
        assert len(refunds) == (1 if status == "cancelled" else 0), "refund count mismatch"
        if refunds:
            assert refunds[0]["amount"] == amount, "refund amount mismatch"
        normalized.append({"status": status, "amount": amount, "items": items})
    assert len(ledger) == sum(1 + (x["status"] == "cancelled") for x in normalized), (
        "orphan/extra ledger rows"
    )
    inventory = {sku: row["stock"] for sku, row in snapshot.get("inventory", {}).items()}
    consumed = {sku: 0 for sku in CATALOG}
    for row in normalized:
        if row["status"] == "paid":
            for item in row["items"]:
                consumed[item["sku"]] += item["quantity"]
    assert set(inventory) == set(CATALOG), "inventory catalog changed"
    for sku, product in CATALOG.items():
        assert inventory[sku] >= 0 and inventory[sku] + consumed[sku] == product["stock"], (
            "stock conservation violation"
        )
    return {
        "orders": sorted(normalized, key=lambda x: json.dumps(x, sort_keys=True)),
        "inventory": inventory,
        "charges": sum(x["amount"] for x in ledger if x["kind"] == "charge"),
        "refunds": sum(x["amount"] for x in ledger if x["kind"] == "refund"),
    }


class Replay:
    def __init__(
        self, source: Path, root: Path, *, trusted=False, old_database=None, new_database=None
    ):
        self.source, self.root = source.resolve(), root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.baseline = Server(v1_app(old_database or str(root / "v1.db")))
        self.successor = Server(v2_app(new_database or str(root / "v2.db")))
        try:
            self.worker = AdapterProcess(
                source, self.successor.url, root / "worker", trusted=trusted
            )
        except BaseException:
            self.baseline.close()
            self.successor.close()
            raise
        self.old, self.new = LegacyClient(self.baseline.url), LegacyClient(self.worker.url)
        self.ids: dict[int, int] = {}
        self.events: list[dict] = []
        self.last: dict | None = None
        self.contract = load(examples() / "contracts/commerce/v1.json")

    def pair(self, name, old_call, new_call):
        start = time.perf_counter()
        expected = old_call()
        old_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        actual = new_call()
        new_ms = (time.perf_counter() - start) * 1000
        expected_data, actual_data = expected.json(), actual.json()
        self.last = {
            "action": name,
            "input": {
                "method": expected.request.method,
                "path": expected.request.url.path,
                "query": str(expected.request.url.query.decode()),
                "body": json.loads(expected.request.content) if expected.request.content else None,
            },
            "expected_status": expected.status_code,
            "actual_status": actual.status_code,
            "expected": expected_data,
            "actual": actual_data,
            "v1_ms": round(old_ms, 3),
            "adapter_ms": round(new_ms, 3),
        }
        self.events.append(self.last)
        assert expected.status_code == actual.status_code, f"{name}: HTTP status mismatch"
        for response in (expected, actual):
            validate_response(
                self.contract, response.request.method, response.request.url.path, response
            )
        if isinstance(expected_data, dict) and "id" in expected_data:
            self.record_id(expected_data["id"], actual_data.get("id"))
        compare_json(self.normalize(expected_data), actual_data)
        return expected_data, actual_data

    def record_id(self, expected_id, candidate_id):
        assert type(candidate_id) is int and candidate_id > 0, (
            "legacy order ID must be a positive integer"
        )
        previous = self.ids.get(expected_id)
        assert previous is None or previous == candidate_id, "unstable ID correspondence"
        assert all(
            key == expected_id or value != candidate_id for key, value in self.ids.items()
        ), "aliased order IDs"
        self.ids[expected_id] = candidate_id

    def normalize(self, value):
        if isinstance(value, list):
            return [self.normalize(x) for x in value]
        if isinstance(value, dict):
            return {
                k: self.ids.get(v, v) if k == "id" and type(v) is int else self.normalize(v)
                for k, v in value.items()
            }
        return value

    def checkout(self, **kwargs):
        return self.pair(
            "checkout", lambda: self.old.checkout(**kwargs), lambda: self.new.checkout(**kwargs)
        )

    def verify_state(self):
        a = effect(self.baseline.app.state.store.snapshot(), 1)
        b = effect(self.successor.app.state.store.snapshot(), 2)
        assert a == b, f"business effects differ: expected={a}, actual={b}"
        return a

    def run_scenario(self, scenario):
        if scenario == "ordinary":
            a, b = self.checkout()
            self.pair("retrieve", lambda: self.old.order(a["id"]), lambda: self.new.order(b["id"]))
            self.pair("list", self.old.orders, self.new.orders)
            self.pair("inventory", self.old.inventory, self.new.inventory)
        elif scenario == "errors":
            for kwargs in (
                {"key": "unknown", "items": [{"sku": "MISSING", "quantity": 1}]},
                {"key": "total", "amount": 1},
                {"key": "declined", "token": "declined"},
                {"key": "stock", "items": [{"sku": "COFFEE", "quantity": 31}], "amount": 38750},
                {"key": "negative", "amount": -1},
                {"key": "badquantity", "items": [{"sku": "COFFEE", "quantity": 0}]},
            ):
                self.checkout(**kwargs)
            self.pair("missing order", lambda: self.old.order(9876), lambda: self.new.order(9876))
            self.pair(
                "missing inventory",
                lambda: self.old.inventory("MISSING"),
                lambda: self.new.inventory("MISSING"),
            )
        elif scenario == "lost_response":
            # A transport response is deliberately discarded at the client boundary on BOTH sides.
            self.old.checkout(key="lost")
            self.new.checkout(key="lost")
            self.checkout(key="lost")
        elif scenario in ("payment_loss_restart", "reserve_failure", "confirmation_loss"):
            point = {
                "payment_loss_restart": "payment.after",
                "reserve_failure": "reserve.before",
                "confirmation_loss": "confirm.after",
            }[scenario]
            self.successor.app.state.faults.arm(point)
            expected = self.old.checkout(key=scenario)
            pending = self.new.checkout(key=scenario)
            self.events.append(
                {
                    "action": "injected " + point,
                    "actual_status": pending.status_code,
                    "actual": pending.json(),
                    "policy": "503 PENDING then retry the same key",
                }
            )
            assert pending.status_code == 503 and pending.json() == {"error": "PENDING"}, (
                "ambiguous failure must be pending"
            )
            if scenario == "payment_loss_restart":
                self.worker.restart()
            actual = self.new.checkout(key=scenario)
            assert expected.status_code == actual.status_code == 201, "retry failed to recover"
            a, b = expected.json(), actual.json()
            self.record_id(a["id"], b.get("id"))
            compare_json(self.normalize(a), b)
        elif scenario == "cancellation":
            a, b = self.checkout(key="cancel")
            for _ in range(2):
                self.pair(
                    "cancel", lambda: self.old.cancel(a["id"]), lambda: self.new.cancel(b["id"])
                )
            self.checkout(key="cancel")
        elif scenario == "pagination":
            # Include failed drafts before the published orders to catch leaking V2 internal records.
            self.checkout(key="failed", token="declined")
            for i in range(5):
                self.checkout(key=f"page-{i}", items=[{"sku": "TEA", "quantity": 1}], amount=750)
            for page in (1, 2, 3, 4):
                self.pair(
                    "page " + str(page),
                    lambda p=page: self.old.orders(p, 2),
                    lambda p=page: self.new.orders(p, 2),
                )
        elif scenario == "concurrent_retry":
            with ThreadPoolExecutor(max_workers=8) as pool:
                old = list(pool.map(lambda _: self.old.checkout(key="parallel"), range(6)))
                new = list(pool.map(lambda _: self.new.checkout(key="parallel"), range(6)))
            assert all(x.status_code == 201 for x in old + new), (
                "concurrent duplicate request failed"
            )
            assert all(x.json() == old[0].json() for x in old), "V1 unstable concurrent results"
            assert all(x.json() == new[0].json() for x in new), (
                "adapter unstable concurrent results"
            )
            self.record_id(old[0].json()["id"], new[0].json().get("id"))
            compare_json(self.normalize(old[0].json()), new[0].json())
            self.events.append(
                {"action": "six concurrent duplicate checkouts", "status": "matched"}
            )
        elif scenario == "key_conflict":
            self.checkout(key="conflict")
            self.checkout(key="conflict", items=[{"sku": "TEA", "quantity": 1}], amount=750)
        elif scenario in ("compensation_retry", "compensation_loss_restart"):
            self.successor.app.state.faults.arm(
                "void.before" if scenario == "compensation_retry" else "void.after"
            )
            old = self.old.checkout(key="compensate", token="declined")
            pending = self.new.checkout(key="compensate", token="declined")
            assert pending.status_code == 503, "failed compensation must stay pending"
            if scenario == "compensation_loss_restart":
                self.worker.restart()
            recovered = self.new.checkout(key="compensate", token="declined")
            assert (
                recovered.status_code == old.status_code == 402 and recovered.json() == old.json()
            )
            self.events.append({"action": "compensation failure then retry", "status": "matched"})
        elif scenario == "cancel_loss_restart":
            a, b = self.checkout(key="cancel-loss")
            self.old.cancel(a["id"])
            self.successor.app.state.faults.arm("void.after")
            assert self.new.cancel(b["id"]).status_code == 503
            self.worker.restart()
            self.pair(
                "retry cancel", lambda: self.old.cancel(a["id"]), lambda: self.new.cancel(b["id"])
            )
        elif scenario == "rejected_key_reuse":
            self.checkout(key="corrected", token="declined")
            self.checkout(key="corrected", token="ok")
        elif scenario == "error_precedence":
            self.checkout(key="multi-invalid", items=[{"sku": "COFFEE", "quantity": 99}], amount=1)
            self.checkout(
                key="unknown-and-total", items=[{"sku": "MISSING", "quantity": 1}], amount=1
            )
            self.checkout(key="total-and-token", amount=1, token="declined")
        else:
            raise ValueError("unknown scenario")
        return self.verify_state()

    def close(self):
        self.old.close()
        self.new.close()
        self.worker.close()
        self.baseline.close()
        self.successor.close()


def evaluate(source: Path, output: Path, *, scenarios=SCENARIOS, trusted=False):
    output.mkdir(parents=True, exist_ok=True)
    checker_path = Path(__file__)
    checker_hash, client_hash = (
        digest(checker_path),
        digest(Path(__file__).parent.parent / "apps/legacy_client.py"),
    )
    original_checker = checker_path.read_bytes()
    rows = []
    for index, scenario in enumerate(scenarios):
        root = output / f"{index:02d}-{scenario}"
        root.mkdir()
        replay = None
        started = time.perf_counter()
        record = {"scenario": scenario, "seed": 0, "status": "fail"}
        try:
            replay = Replay(source, root, trusted=trusted)
            record["before"] = {
                "v1": replay.baseline.app.state.store.snapshot(),
                "v2": replay.successor.app.state.store.snapshot(),
            }
            record["effects"] = replay.run_scenario(scenario)
            record["status"] = "pass"
            record["isolation"] = replay.worker.isolation
        except Exception as exc:
            error = exc
            if replay:
                try:
                    replay.worker.ensure_alive()
                except WorkerFailure as failure:
                    error = failure
            record["failure_kind"] = (
                "worker_infrastructure"
                if isinstance(error, WorkerFailure)
                else "semantic_or_trial_error"
            )
            record["mismatch"] = f"{type(error).__name__}: {error}"
        finally:
            if replay:
                record["actions"] = replay.events
                record["after"] = {
                    "v1": replay.baseline.app.state.store.snapshot(),
                    "v2": replay.successor.app.state.store.snapshot(),
                }
                replay.close()
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        rows.append(record)
        (root / "result.json").write_text(json.dumps(record, indent=2))
    if digest(checker_path) != checker_hash:
        checker_path.write_bytes(original_checker)
        raise RuntimeError("checker integrity violation; restored original")
    assert digest(Path(__file__).parent.parent / "apps/legacy_client.py") == client_hash, (
        "legacy client changed"
    )
    report = {
        "suite": "commerce",
        "artifact_sha256": digest(source),
        "checker_sha256": checker_hash,
        "legacy_client_sha256": client_hash,
        "seed": 0,
        "scenarios": rows,
        "passed": sum(x["status"] == "pass" for x in rows),
        "total": len(rows),
        "replay": f"parity validate --adapter {source.parent} --suite commerce",
    }
    (output / "evaluation.json").write_text(json.dumps(report, indent=2))
    return report
