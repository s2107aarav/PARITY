"""Checker calibration: preserve defective candidates and verify real rejection."""

import json
from pathlib import Path

from parity.replay import evaluate


def mutants(reference: Path):
    healthy = reference.read_text()
    return {
        "symptom_only_success": """def handle(ctx, method, path, body, query, headers):
    if method == "POST" and path == "/checkout":
        return 201, {"id": 1, "status": "paid", "amount": body["amount"], "items": body["items"]}
    if method == "GET" and path == "/orders/1":
        return 200, {"id": 1, "status": "paid", "amount": 1250, "items": [{"sku": "COFFEE", "quantity": 1}]}
    if path == "/orders":
        return 200, {"orders": [{"id": 1, "status": "paid", "amount": 1250, "items": [{"sku": "COFFEE", "quantity": 1}]}], "total": 1, "page": 1, "page_size": 2}
    return 200, {"sku": "COFFEE", "available": 29}
""",
        "wrong_money": healthy.replace(
            '"amount": order["total_minor"]', '"amount": order["total_minor"] + 1'
        ),
        "duplicate_charge": healthy
        + """
original_handle = handle
def handle(ctx, method, path, body, query, headers):
    result = original_handle(ctx, method, path, body, query, headers)
    if method == "POST" and path == "/checkout" and result[0] == 201:
        duplicate_headers = dict(headers)
        duplicate_headers["idempotency-key"] = headers["idempotency-key"] + "-duplicate"
        original_handle(ctx, method, path, body, query, duplicate_headers)
    return result
""",
        "stock_leak": healthy
        + """
original_handle = handle
def handle(ctx, method, path, body, query, headers):
    result = original_handle(ctx, method, path, body, query, headers)
    if method == "POST" and path == "/checkout" and result[0] == 201:
        with ctx.lock():
            draft = ctx.call("POST", "/orders", {"lines": [{"product_code": "TEA", "units": 1}], "total_minor": 750}, headers={"Idempotency-Key": "leak"})
            ctx.call("POST", "/inventory/reservations", {"order_ref": draft["order_ref"]})
    return result
""",
        "wrong_error": healthy.replace(
            '"DECLINED": (402, "PAYMENT_DECLINED")', '"DECLINED": (500, "GENERIC_ERROR")'
        ),
    }


def calibrate(reference: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, source in mutants(reference).items():
        root = output / name
        root.mkdir()
        candidate = root / "adapter.py"
        candidate.write_text(source)
        report = evaluate(
            candidate,
            root / "trial",
            scenarios=("errors",) if name == "wrong_error" else ("ordinary",),
        )
        rows.append(
            {
                "mutant": name,
                "rejected": report["passed"] == 0
                and report["scenarios"][0].get("failure_kind") != "worker_infrastructure",
                "mismatch": report["scenarios"][0].get("mismatch"),
                "artifact": str(candidate),
            }
        )
    result = {"rejected": sum(x["rejected"] for x in rows), "total": len(rows), "mutants": rows}
    (output / "calibration.json").write_text(json.dumps(result, indent=2))
    return result
