"""Atomic legacy commerce API: no calls to, or state shared with, V2."""

from fastapi import FastAPI, Header, Query

from apps.common import ApiError, Checkout, Item, StrictModel, fingerprint, seed, setup
from parity.storage import Store


class LegacyOrder(StrictModel):
    id: int
    status: str
    amount: int
    items: list[Item]


class OrderPage(StrictModel):
    orders: list[LegacyOrder]
    total: int
    page: int
    page_size: int


class Stock(StrictModel):
    sku: str
    available: int


def create_app(database: str) -> FastAPI:
    app = FastAPI(title="Commerce V1", version="1.0.0")
    store = Store(database)
    seed(store)
    app.state.store = store
    setup(app, 1)

    @app.post("/checkout", operation_id="checkout", status_code=201, response_model=LegacyOrder)
    def checkout(body: Checkout, idempotency_key: str = Header(min_length=1, max_length=120)):
        """Atomic checkout. Same key/body returns same order; different body is 409.
        Amount uses minor units; tokens ok/declined. Rejected attempts have no business effects.
        """
        value = body.model_dump()
        with store.transaction() as tx:
            prior = tx.get("requests", idempotency_key)
            if prior:
                if prior["fingerprint"] != fingerprint(value):
                    raise ApiError(409, "KEY_REUSED")
                return tx.get("orders", prior["id"])
            quantities: dict[str, int] = {}
            for item in body.items:
                quantities[item.sku] = quantities.get(item.sku, 0) + item.quantity
            total = 0
            for sku, count in quantities.items():
                product = tx.get("inventory", sku)
                if product is None:
                    raise ApiError(404, "UNKNOWN_SKU")
                total += product["price"] * count
                if product["stock"] < count:
                    raise ApiError(409, "OUT_OF_STOCK")
            if total != body.amount:
                raise ApiError(422, "AMOUNT_MISMATCH")
            if body.payment_token != "ok":
                raise ApiError(402, "PAYMENT_DECLINED")
            oid = tx.next_id("orders")
            order = {
                "id": oid,
                "status": "paid",
                "amount": total,
                "items": [x.model_dump() for x in body.items],
            }
            for sku, count in quantities.items():
                product = tx.get("inventory", sku)
                product["stock"] -= count
                tx.put("inventory", sku, product)
            tx.put("orders", oid, order)
            tx.put(
                "payments", f"charge:{oid}", {"order_id": oid, "kind": "charge", "amount": total}
            )
            tx.put("requests", idempotency_key, {"id": oid, "fingerprint": fingerprint(value)})
            return order

    @app.get("/orders", operation_id="list_orders", response_model=OrderPage)
    def orders(page: int = Query(1, ge=1), page_size: int = Query(2, ge=1, le=100)):
        """Ascending creation order; cancelled orders remain visible."""
        with store.transaction() as tx:
            rows = sorted(tx.all("orders"), key=lambda x: x["id"])
            return {
                "orders": rows[(page - 1) * page_size : page * page_size],
                "total": len(rows),
                "page": page,
                "page_size": page_size,
            }

    @app.get("/orders/{order_id}", operation_id="get_order", response_model=LegacyOrder)
    def get_order(order_id: int):
        with store.transaction() as tx:
            order = tx.get("orders", order_id)
            if order is None:
                raise ApiError(404, "NOT_FOUND")
            return order

    @app.post("/orders/{order_id}/cancel", operation_id="cancel_order", response_model=LegacyOrder)
    def cancel(order_id: int):
        """Idempotent cancellation; refund once and return all stock."""
        with store.transaction() as tx:
            order = tx.get("orders", order_id)
            if order is None:
                raise ApiError(404, "NOT_FOUND")
            if order["status"] != "cancelled":
                for item in order["items"]:
                    product = tx.get("inventory", item["sku"])
                    product["stock"] += item["quantity"]
                    tx.put("inventory", item["sku"], product)
                order["status"] = "cancelled"
                tx.put("orders", order_id, order)
                tx.put(
                    "payments",
                    f"refund:{order_id}",
                    {"order_id": order_id, "kind": "refund", "amount": order["amount"]},
                )
            return order

    @app.get("/inventory/{sku}", operation_id="get_inventory", response_model=Stock)
    def inventory(sku: str):
        with store.transaction() as tx:
            product = tx.get("inventory", sku)
            if product is None:
                raise ApiError(404, "UNKNOWN_SKU")
            return {"sku": sku, "available": product["stock"]}

    return app
