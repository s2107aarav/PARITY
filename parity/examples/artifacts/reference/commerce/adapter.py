"""Commerce reference adapter used to calibrate the independent checker."""

import re
from urllib.parse import quote, unquote

from parity.runtime import UpstreamError

ERRORS = {
    "PRODUCT_MISSING": (404, "UNKNOWN_SKU"),
    "BAD_TOTAL": (422, "AMOUNT_MISMATCH"),
    "INSUFFICIENT_STOCK": (409, "OUT_OF_STOCK"),
    "DECLINED": (402, "PAYMENT_DECLINED"),
    "KEY_CONFLICT": (409, "KEY_REUSED"),
    "MISSING_RESOURCE": (404, "NOT_FOUND"),
    "INVALID_INPUT": (422, "INVALID"),
    "ORDER_CLOSED": (409, "CANCELLED"),
}


def legacy(ctx, order):
    return {
        "id": ctx.id_for(order["order_ref"]),
        "status": "cancelled" if order["phase"] == "cancelled" else "paid",
        "amount": order["total_minor"],
        "items": [
            {"sku": line["product_code"], "quantity": line["units"]} for line in order["lines"]
        ],
    }


def valid(body):
    if not isinstance(body, dict) or set(body) != {"items", "amount", "payment_token"}:
        return False
    if (
        type(body["amount"]) is not int
        or body["amount"] <= 0
        or not isinstance(body["payment_token"], str)
    ):
        return False
    items = body["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        return False
    return all(
        isinstance(x, dict)
        and set(x) == {"sku", "quantity"}
        and isinstance(x["sku"], str)
        and type(x["quantity"]) is int
        and 0 < x["quantity"] <= 10000
        for x in items
    )


def upstream_error(exc):
    if exc.status >= 500:
        return 503, "PENDING"
    problem = exc.data.get("problem", {}) if isinstance(exc.data, dict) else {}
    code = problem.get("kind") if isinstance(problem, dict) else None
    return ERRORS.get(code, (502, "UPSTREAM_ERROR"))


def void_failed_checkout(ctx, state_key, state):
    try:
        ctx.call("POST", "/orders/" + state["ref"] + "/void")
    except UpstreamError:
        return 503, {"error": "PENDING"}
    state["error"] = state["failure"]
    state["compensating"] = False
    ctx.put(state_key, state)
    return state["error"]


def fail_checkout(ctx, state_key, state, status, code):
    error = [status, {"error": code}]
    if status == 503:
        return error
    if "ref" in state:
        state.update(compensating=True, failure=error)
        ctx.put(state_key, state)
        return void_failed_checkout(ctx, state_key, state)
    state["error"] = error
    ctx.put(state_key, state)
    return error


def checkout(ctx, body, headers):
    key = headers.get("idempotency-key", "")
    if not key or len(key) > 120 or not valid(body):
        return 422, {"error": "INVALID"}
    state_key = "checkout:" + key
    digest = ctx.digest(body)
    state = ctx.get(state_key)
    if not state or state.get("error"):
        state = {"fingerprint": digest, "generation": state["generation"] + 1 if state else 1}
        ctx.put(state_key, state)
    if state["fingerprint"] != digest:
        return 409, {"error": "KEY_REUSED"}
    if state.get("compensating"):
        return void_failed_checkout(ctx, state_key, state)
    if state.get("done"):
        order = ctx.call("GET", "/orders/" + state["ref"])
        return 201, legacy(ctx, order)
    upstream_key = str(state["generation"]) + ":" + key
    try:
        if not state.get("preflight"):
            counts = {}
            for item in body["items"]:
                sku = item["sku"]
                counts[sku] = counts.get(sku, 0) + item["quantity"]
            for sku, quantity in counts.items():
                if not sku or "/" in sku or sku in {".", ".."}:
                    return fail_checkout(ctx, state_key, state, 404, "UNKNOWN_SKU")
                stock = ctx.call("GET", "/stock/" + quote(sku, safe=""))
                if stock["units_available"] < quantity:
                    return fail_checkout(ctx, state_key, state, 409, "OUT_OF_STOCK")
            state["preflight"] = True
            ctx.put(state_key, state)
        if "ref" not in state:
            draft = ctx.call(
                "POST",
                "/orders",
                {
                    "lines": [
                        {"product_code": x["sku"], "units": x["quantity"]} for x in body["items"]
                    ],
                    "total_minor": body["amount"],
                },
                headers={"Idempotency-Key": "draft:" + upstream_key},
            )
            state["ref"] = draft["order_ref"]
            ctx.put(state_key, state)
        ref = state["ref"]
        if not state.get("reserved"):
            ctx.call("POST", "/inventory/reservations", {"order_ref": ref})
            state["reserved"] = True
            ctx.put(state_key, state)
        if not state.get("paid"):
            ctx.call(
                "POST",
                "/payments",
                {"order_ref": ref, "total_minor": body["amount"], "token": body["payment_token"]},
                headers={"Idempotency-Key": "payment:" + upstream_key},
            )
            state["paid"] = True
            ctx.put(state_key, state)
        order = ctx.call("POST", "/orders/" + ref + "/confirm")
        ctx.id_for(ref)
        state["done"] = True
        ctx.put(state_key, state)
        return 201, legacy(ctx, order)
    except UpstreamError as exc:
        return fail_checkout(ctx, state_key, state, *upstream_error(exc))


def integer(value):
    if isinstance(value, list):
        value = value[-1]
    value = value.strip()
    if not re.fullmatch(r"[+-]?[0-9](?:_?[0-9])*(?:\.0+)?", value):
        raise ValueError("invalid integer")
    return int(value.split(".", 1)[0])


def handle(ctx, method, path, body, query, headers):
    path = unquote(path)
    with ctx.lock():
        try:
            if method == "POST" and path == "/checkout":
                return checkout(ctx, body, headers)
            parts = path[1:].split("/")
            if method == "GET" and len(parts) == 2 and parts[0] == "inventory":
                value = ctx.call("GET", "/stock/" + quote(parts[1], safe=""))
                return 200, {"sku": value["product_code"], "available": value["units_available"]}
            if method == "GET" and path == "/orders":
                page, size = integer(query.get("page", "1")), integer(query.get("page_size", "2"))
                if page < 1 or not 1 <= size <= 100:
                    return 422, {"error": "INVALID"}
                # Only published legacy IDs are visible: failed V2 drafts stay internal.
                rows = [legacy(ctx, ctx.call("GET", "/orders/" + ref)) for ref in ctx.mappings()]
                rows.sort(key=lambda x: x["id"])
                return 200, {
                    "orders": rows[(page - 1) * size : page * size],
                    "total": len(rows),
                    "page": page,
                    "page_size": size,
                }
            if len(parts) >= 2 and parts[0] == "orders":
                oid = integer(parts[1])
                ref = ctx.upstream_id(oid)
                if ref is None:
                    return 404, {"error": "NOT_FOUND"}
                if method == "GET" and len(parts) == 2:
                    return 200, legacy(ctx, ctx.call("GET", "/orders/" + ref))
                if method == "POST" and len(parts) == 3 and parts[2] == "cancel":
                    return 200, legacy(ctx, ctx.call("POST", "/orders/" + ref + "/void"))
            return 404, {"error": "NOT_FOUND"}
        except (ValueError, TypeError):
            return 422, {"error": "INVALID"}
        except UpstreamError as exc:
            status, code = upstream_error(exc)
            return status, {"error": code}
