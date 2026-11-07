from fastapi.testclient import TestClient

from apps.commerce_v1 import create_app as v1
from apps.commerce_v2 import create_app as v2
from parity.replay import effect


def test_v2_breaks_unchanged_client_contract(tmp_path):
    body = {"items": [{"sku": "COFFEE", "quantity": 1}], "amount": 1250, "payment_token": "ok"}
    with TestClient(v1(str(tmp_path / "v1.db"))) as a, TestClient(v2(str(tmp_path / "v2.db"))) as b:
        assert a.post("/checkout", json=body, headers={"Idempotency-Key": "one"}).status_code == 201
        assert b.post("/checkout", json=body, headers={"Idempotency-Key": "one"}).status_code == 404


def test_v2_payment_reconciliation_and_refund(tmp_path):
    app = v2(str(tmp_path / "v2.db"))
    with TestClient(app) as client:
        oid = client.post(
            "/orders",
            json={"lines": [{"product_code": "COFFEE", "units": 2}], "total_minor": 2500},
            headers={"Idempotency-Key": "draft"},
        ).json()["order_ref"]
        assert client.post(f"/orders/{oid}/confirm").status_code == 409
        assert client.post("/inventory/reservations", json={"order_ref": oid}).status_code == 200
        app.state.faults.arm("payment.after")
        body = {"order_ref": oid, "total_minor": 2500, "token": "ok"}
        assert (
            client.post("/payments", json=body, headers={"Idempotency-Key": "pay"}).status_code
            == 503
        )
        for _ in range(3):
            assert (
                client.post("/payments", json=body, headers={"Idempotency-Key": "pay"}).status_code
                == 200
            )
        assert client.post(f"/orders/{oid}/confirm").status_code == 200
        for _ in range(2):
            assert client.post(f"/orders/{oid}/void").status_code == 200
        state = effect(app.state.store.snapshot(), 2)
        assert state["charges"] == state["refunds"] == 2500
        assert state["inventory"]["COFFEE"] == 30
        # Stale draft cannot reacquire stock or confirm after void.
        assert client.post("/inventory/reservations", json={"order_ref": oid}).status_code == 409
        assert client.post(f"/orders/{oid}/confirm").status_code == 409
    app.state.store.close()


def test_storage_transaction_rolls_back(tmp_path):
    from parity.storage import Store

    store = Store(str(tmp_path / "state.db"))
    try:
        with store.transaction() as tx:
            tx.put("test", "x", {"value": 1})
            raise RuntimeError("interrupted")
    except RuntimeError:
        pass
    assert store.snapshot() == {}
    store.close()


def test_payment_retry_binds_every_successful_key(tmp_path):
    app = v2(str(tmp_path / "service.db"))
    try:
        with TestClient(app) as client:

            def reserved_order(key):
                order = client.post(
                    "/orders",
                    json={"lines": [{"product_code": "COFFEE", "units": 1}], "total_minor": 1250},
                    headers={"Idempotency-Key": key},
                ).json()["order_ref"]
                assert (
                    client.post("/inventory/reservations", json={"order_ref": order}).status_code
                    == 200
                )
                return order

            first, second = reserved_order("first"), reserved_order("second")
            body = {"order_ref": first, "total_minor": 1250, "token": "ok"}
            charge = client.post("/payments", json=body, headers={"Idempotency-Key": "original"})
            retried = client.post("/payments", json=body, headers={"Idempotency-Key": "alias"})
            assert charge.status_code == retried.status_code == 200
            assert charge.json() == retried.json()
            conflict = client.post(
                "/payments",
                json={**body, "order_ref": second},
                headers={"Idempotency-Key": "alias"},
            )
            assert conflict.status_code == 409 and conflict.json() == {
                "problem": {"kind": "KEY_CONFLICT"}
            }
            assert len(app.state.store.snapshot()["payments"]) == 1
    finally:
        app.state.store.close()
