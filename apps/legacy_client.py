"""Fixed V1 client. Never imports the adapter or checks which server it is using."""

import httpx


class LegacyClient:
    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=15, trust_env=False)

    def checkout(self, key="cart-1", items=None, amount=1250, token="ok"):
        return self.http.post(
            "/checkout",
            headers={"Idempotency-Key": key},
            json={
                "items": items if items is not None else [{"sku": "COFFEE", "quantity": 1}],
                "amount": amount,
                "payment_token": token,
            },
        )

    def order(self, order_id):
        return self.http.get(f"/orders/{order_id}")

    def orders(self, page=1, page_size=2):
        return self.http.get("/orders", params={"page": page, "page_size": page_size})

    def cancel(self, order_id):
        return self.http.post(f"/orders/{order_id}/cancel")

    def inventory(self, sku="COFFEE"):
        return self.http.get(f"/inventory/{sku}")

    def close(self):
        self.http.close()
