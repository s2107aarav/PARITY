"""Public diagnostics, intentionally separate from the private acceptance checker."""

import json
from pathlib import Path

import httpx

from parity.fixtures import FIXTURES
from parity.sandbox import AdapterProcess
from parity.serving import Server


def validate(source: Path, domain: str, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    service = Server(FIXTURES[domain].new(str(root / "v2.db")))
    worker = None
    steps = []
    try:
        worker = AdapterProcess(source, service.url, root / "worker")
        with httpx.Client(base_url=worker.url, trust_env=False, timeout=15) as client:
            if domain == "commerce":
                body = {
                    "items": [{"sku": "COFFEE", "quantity": 1}],
                    "amount": 1250,
                    "payment_token": "ok",
                }
                first = client.post("/checkout", json=body, headers={"Idempotency-Key": "public"})
                steps.append(
                    {"request": "POST /checkout", "status": first.status_code, "body": first.json()}
                )
                assert first.status_code == 201 and first.json()["status"] == "paid", (
                    "ordinary checkout failed"
                )
                oid = first.json()["id"]
                got = client.get(f"/orders/{oid}")
                assert got.json() == first.json(), "retrieve differs from checkout"
                declined = client.post(
                    "/checkout",
                    json={**body, "payment_token": "declined"},
                    headers={"Idempotency-Key": "declined"},
                )
                steps.append(
                    {
                        "request": "declined checkout",
                        "status": declined.status_code,
                        "body": declined.json(),
                    }
                )
                assert declined.status_code == 402 and declined.json() == {
                    "error": "PAYMENT_DECLINED"
                }, "decline mapping failed"
                service.app.state.faults.arm("payment.after")
                pending = client.post("/checkout", json=body, headers={"Idempotency-Key": "loss"})
                steps.append(
                    {
                        "request": "lost payment reply",
                        "status": pending.status_code,
                        "body": pending.json(),
                    }
                )
                assert pending.status_code == 503 and pending.json() == {"error": "PENDING"}, (
                    "ambiguous capture must be pending"
                )
                worker.restart()
                retried = client.post("/checkout", json=body, headers={"Idempotency-Key": "loss"})
                assert retried.status_code == 201, "retry after restart failed"
                ledger = list(service.app.state.store.snapshot().get("payments", {}).values())
                assert len([x for x in ledger if x["kind"] == "charge"]) == 2, (
                    "duplicate/missing payment in public workflow"
                )
                # Publicly documented precedence: legacy stock validation precedes amount validation.
                mixed = client.post(
                    "/checkout",
                    json={
                        "items": [{"sku": "COFFEE", "quantity": 99}],
                        "amount": 1,
                        "payment_token": "ok",
                    },
                    headers={"Idempotency-Key": "mixed-invalid"},
                )
                steps.append(
                    {
                        "request": "insufficient stock AND wrong amount",
                        "status": mixed.status_code,
                        "body": mixed.json(),
                        "expected": {"status": 409, "body": {"error": "OUT_OF_STOCK"}},
                    }
                )
                assert mixed.status_code == 409 and mixed.json() == {"error": "OUT_OF_STOCK"}, (
                    "legacy error precedence differs: V1 checks stock before amount; actual result above"
                )
            elif domain == "transfer":
                body = {"title": "Login broken", "body": "Cannot sign in"}
                first = client.post("/tickets", json=body, headers={"Idempotency-Key": "public"})
                steps.append(
                    {"request": "POST /tickets", "status": first.status_code, "body": first.json()}
                )
                assert first.status_code == 201 and first.json()["body"] == body["body"]
                assert client.get(f"/tickets/{first.json()['id']}").json() == first.json()
                service.app.state.faults.arm("publish.after")
                pending = client.post("/tickets", json=body, headers={"Idempotency-Key": "loss"})
                assert pending.status_code == 503
                worker.restart()
                retried = client.post("/tickets", json=body, headers={"Idempotency-Key": "loss"})
                assert retried.status_code == 201
                rows = list(service.app.state.store.snapshot()["issues"].values())
                assert len(rows) == 2 and all(x["comments"] == [body["body"]] for x in rows)
            else:
                check = FIXTURES[domain].public_check
                if check is None:
                    raise ValueError("no public diagnostics registered")
                check(client, service, worker)
        result = {"passed": True, "steps": steps, "scope": "public smoke checks only"}
    except Exception as exc:
        result = {
            "passed": False,
            "steps": steps,
            "diagnostic": f"{type(exc).__name__}: {exc}",
            "scope": "public smoke checks only",
        }
    finally:
        if worker:
            worker.close()
        service.close()
    (root / "public-result.json").write_text(json.dumps(result, indent=2))
    return result
