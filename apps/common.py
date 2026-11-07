"""Transport schemas and catalog only; checkout implementations are independent."""

import hashlib
import json
import threading
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

CATALOG = {"COFFEE": {"price": 1250, "stock": 30}, "TEA": {"price": 750, "stock": 20}}
Positive = Annotated[int, Field(strict=True, gt=0, le=10000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Item(StrictModel):
    sku: str
    quantity: Positive


class Checkout(StrictModel):
    items: Annotated[list[Item], Field(min_length=1, max_length=20)]
    amount: Annotated[int, Field(strict=True, gt=0)]
    payment_token: str


class ApiError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Faults:
    """Harness-owned one-shot faults, never a public HTTP endpoint."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()

    def arm(self, point: str):
        with self.lock:
            self.counts[point] = self.counts.get(point, 0) + 1

    def hit(self, point: str):
        with self.lock:
            if self.counts.get(point, 0):
                self.counts[point] -= 1
                raise ApiError(503, "TEMPORARY")


def setup(app: FastAPI, version: int):
    app.state.faults = Faults()

    @app.exception_handler(ApiError)
    async def business_error(_request: Request, exc: ApiError):
        data = {"error": exc.code} if version == 1 else {"problem": {"kind": exc.code}}
        return JSONResponse(data, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return JSONResponse(
            {"error": "INVALID"} if version == 1 else {"problem": {"kind": "INVALID_INPUT"}},
            status_code=422,
        )


def seed(store):
    with store.transaction() as tx:
        for sku, product in CATALOG.items():
            if tx.get("inventory", sku) is None:
                tx.put("inventory", sku, {"sku": sku, **product})
