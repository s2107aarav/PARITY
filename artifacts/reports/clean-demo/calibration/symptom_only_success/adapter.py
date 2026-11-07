def handle(ctx, method, path, body, query, headers):
    if method == "POST" and path == "/checkout":
        return 201, {"id": 1, "status": "paid", "amount": body["amount"], "items": body["items"]}
    if method == "GET" and path == "/orders/1":
        return 200, {"id": 1, "status": "paid", "amount": 1250, "items": [{"sku": "COFFEE", "quantity": 1}]}
    if path == "/orders":
        return 200, {"orders": [{"id": 1, "status": "paid", "amount": 1250, "items": [{"sku": "COFFEE", "quantity": 1}]}], "total": 1, "page": 1, "page_size": 2}
    return 200, {"sku": "COFFEE", "available": 29}
