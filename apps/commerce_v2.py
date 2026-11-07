"""Primitive commerce service. Every step commits independently and is retryable."""

import uuid
from typing import Annotated

from fastapi import FastAPI, Header, Query
from pydantic import Field

from apps.common import ApiError, Positive, StrictModel, fingerprint, seed, setup
from parity.storage import Store


class Line(StrictModel):
    product_code: str
    units: Positive


class Draft(StrictModel):
    lines: Annotated[list[Line], Field(min_length=1, max_length=20)]
    total_minor: Annotated[int, Field(strict=True, gt=0)]


class OrderRef(StrictModel):
    order_ref: str


class Payment(OrderRef):
    total_minor: Annotated[int, Field(strict=True, gt=0)]
    token: str


class Order(Draft):
    order_ref: str
    phase: str
    sequence: int


class CursorPage(StrictModel):
    data: list[Order]
    next_cursor: int | None


def create_app(database: str) -> FastAPI:
    app = FastAPI(title="Commerce V2", version="2.0.0")
    store = Store(database)
    seed(store)
    app.state.store = store
    setup(app, 2)

    def find(tx, oid):
        order = tx.get("orders", oid)
        if order is None:
            raise ApiError(404, "MISSING_RESOURCE")
        return order

    @app.post("/orders", operation_id="create_draft", status_code=201, response_model=Order)
    def draft(body: Draft, idempotency_key: str = Header(min_length=1, max_length=160)):
        """Idempotent draft; validates catalog and total before any effects. UUID order_ref.
        A reused key with different input is KEY_CONFLICT. Stock is checked during reserve.
        """
        value = body.model_dump()
        with store.transaction() as tx:
            prior = tx.get("requests", idempotency_key)
            if prior:
                if prior["fingerprint"] != fingerprint(value):
                    raise ApiError(409, "KEY_CONFLICT")
                return find(tx, prior["ref"])
            total = 0
            for line in body.lines:
                product = tx.get("inventory", line.product_code)
                if product is None:
                    raise ApiError(404, "PRODUCT_MISSING")
                total += product["price"] * line.units
            if total != body.total_minor:
                raise ApiError(422, "BAD_TOTAL")
            oid = str(uuid.uuid4())
            order = {"order_ref": oid, "phase": "draft", **value, "sequence": tx.next_id("orders")}
            tx.put("orders", oid, order)
            tx.put("requests", idempotency_key, {"ref": oid, "fingerprint": fingerprint(value)})
            return order

    @app.post("/inventory/reservations", operation_id="reserve_stock")
    def reserve(body: OrderRef):
        """At most one reservation per order. Repeating a held reservation never debits stock again."""
        app.state.faults.hit("reserve.before")
        with store.transaction() as tx:
            order = find(tx, body.order_ref)
            if order["phase"] == "cancelled":
                raise ApiError(409, "ORDER_CLOSED")
            prior = tx.get("reservations", body.order_ref)
            if prior:
                return prior
            counts: dict[str, int] = {}
            for line in order["lines"]:
                counts[line["product_code"]] = counts.get(line["product_code"], 0) + line["units"]
            for sku, count in counts.items():
                if tx.get("inventory", sku)["stock"] < count:
                    raise ApiError(409, "INSUFFICIENT_STOCK")
            for sku, count in counts.items():
                product = tx.get("inventory", sku)
                product["stock"] -= count
                tx.put("inventory", sku, product)
            value = {"order_ref": body.order_ref, "state": "held", "quantities": counts}
            tx.put("reservations", body.order_ref, value)
        app.state.faults.hit("reserve.after")
        return value

    @app.post("/payments", operation_id="capture_payment")
    def pay(body: Payment, idempotency_key: str = Header(min_length=1, max_length=160)):
        """Simulated capture. Key AND order are unique; same order/input returns same charge.
        On 503 after commit retry the same input/key. DECLINED has no charge. No real money.
        """
        app.state.faults.hit("payment.before")
        with store.transaction() as tx:
            order = find(tx, body.order_ref)
            prior = tx.get("payment_keys", idempotency_key)
            digest = fingerprint(body.model_dump())
            if prior and prior["fingerprint"] != digest:
                raise ApiError(409, "KEY_CONFLICT")
            charge = tx.get("payments", "charge:" + body.order_ref)
            if charge:
                if charge["fingerprint"] != digest:
                    raise ApiError(409, "KEY_CONFLICT")
                tx.put("payment_keys", idempotency_key, {"fingerprint": digest})
                return charge
            if order["phase"] == "cancelled":
                raise ApiError(409, "ORDER_CLOSED")
            if body.total_minor != order["total_minor"]:
                raise ApiError(422, "BAD_TOTAL")
            if not tx.get("reservations", body.order_ref):
                raise ApiError(409, "RESERVATION_REQUIRED")
            if body.token != "ok":
                raise ApiError(402, "DECLINED")
            charge = {
                "order_ref": body.order_ref,
                "kind": "charge",
                "amount": body.total_minor,
                "fingerprint": digest,
                "payment_ref": str(uuid.uuid4()),
            }
            tx.put("payments", "charge:" + body.order_ref, charge)
            tx.put("payment_keys", idempotency_key, {"fingerprint": digest})
        app.state.faults.hit("payment.after")
        return charge

    @app.post("/orders/{order_ref}/confirm", operation_id="confirm_order", response_model=Order)
    def confirm(order_ref: str):
        """Confirm only after reservation and payment; retry is idempotent."""
        app.state.faults.hit("confirm.before")
        with store.transaction() as tx:
            order = find(tx, order_ref)
            if order["phase"] == "cancelled":
                raise ApiError(409, "ORDER_CLOSED")
            if not tx.get("reservations", order_ref) or not tx.get(
                "payments", "charge:" + order_ref
            ):
                raise ApiError(409, "PREREQUISITES_MISSING")
            order["phase"] = "confirmed"
            tx.put("orders", order_ref, order)
        app.state.faults.hit("confirm.after")
        return order

    @app.post("/orders/{order_ref}/void", operation_id="void_order", response_model=Order)
    def void(order_ref: str):
        """Atomic within V2: release reservation and refund once. Also compensates failed drafts."""
        app.state.faults.hit("void.before")
        with store.transaction() as tx:
            order = find(tx, order_ref)
            if order["phase"] != "cancelled":
                reservation = tx.get("reservations", order_ref)
                if reservation and reservation["state"] == "held":
                    for sku, count in reservation["quantities"].items():
                        product = tx.get("inventory", sku)
                        product["stock"] += count
                        tx.put("inventory", sku, product)
                    reservation["state"] = "released"
                    tx.put("reservations", order_ref, reservation)
                charge = tx.get("payments", "charge:" + order_ref)
                if charge:
                    tx.put(
                        "payments",
                        "refund:" + order_ref,
                        {"order_ref": order_ref, "kind": "refund", "amount": charge["amount"]},
                    )
                order["phase"] = "cancelled"
                tx.put("orders", order_ref, order)
        app.state.faults.hit("void.after")
        return order

    @app.get("/orders", operation_id="list_orders", response_model=CursorPage)
    def orders(cursor: int = Query(0, ge=0), limit: int = Query(2, ge=1, le=100)):
        """Stable ascending sequence cursor; includes internal drafts and cancelled drafts.
        next_cursor is the final sequence if more rows remain, otherwise null.
        """
        with store.transaction() as tx:
            rows = sorted(tx.all("orders"), key=lambda x: x["sequence"])
            remaining = [x for x in rows if x["sequence"] > cursor]
            chosen = remaining[:limit]
            return {
                "data": chosen,
                "next_cursor": chosen[-1]["sequence"] if len(remaining) > limit else None,
            }

    @app.get("/orders/{order_ref}", operation_id="get_order", response_model=Order)
    def get_order(order_ref: str):
        with store.transaction() as tx:
            return find(tx, order_ref)

    @app.get("/stock/{product_code}", operation_id="get_stock")
    def inventory(product_code: str):
        with store.transaction() as tx:
            product = tx.get("inventory", product_code)
            if product is None:
                raise ApiError(404, "PRODUCT_MISSING")
            return {"product_code": product_code, "units_available": product["stock"]}

    return app
