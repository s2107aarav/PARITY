"""Engine-owned plugin ABI. No commerce mappings or workflow policy live here."""

import fcntl
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

from parity.storage import Store


class UpstreamError(Exception):
    def __init__(self, status, data):
        super().__init__(f"upstream HTTP {status}")
        self.status, self.data = status, data


def redact(value):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if any(
                s in k.lower() for s in ("token", "secret", "authorization", "api_key", "password")
            )
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


class Context:
    """Capabilities: bounded upstream HTTP, durable JSON and identifier correspondence.

    Callers must lock around read/modify/write sequences. Local deployment is single-host;
    OS advisory locks serialize multiple adapter processes sharing a database directory.
    """

    def __init__(self, upstream: str, database: str, trace: str | None = None):
        self.store = Store(database)
        self.base = Path(database).resolve().parent
        self.http = httpx.Client(base_url=upstream, timeout=3, trust_env=False)
        self.trace = Path(trace) if trace else self.base / "http.jsonl"
        self.thread_lock = threading.RLock()
        self.local = threading.local()

    @contextmanager
    def lock(self, _key="global"):
        # A global lock avoids checkout/cancel/list races. Throughput is intentionally bounded.
        with self.thread_lock, (self.base / "adapter.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                self.local.calls = 0
                self.local.trace_id = uuid.uuid4().hex
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def call(self, method: str, path: str, body=None, headers=None, query=None):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("only relative upstream paths are allowed")
        self.local.calls = getattr(self.local, "calls", 0) + 1
        if self.local.calls > 80:
            raise UpstreamError(503, {"problem": {"kind": "CALL_BUDGET"}})
        start = time.perf_counter()
        try:
            response = self.http.request(method, path, json=body, headers=headers, params=query)
            data = response.json()
            status = response.status_code
        except (httpx.HTTPError, ValueError):
            data, status = {"problem": {"kind": "TEMPORARY"}}, 503
        event = {
            "trace_id": getattr(self.local, "trace_id", None),
            "call_sequence": self.local.calls,
            "method": method,
            "path": path,
            "body": redact(body),
            "status": status,
            "response": redact(data),
            "ms": round((time.perf_counter() - start) * 1000, 3),
        }
        with self.trace.open("a") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
        if status >= 400:
            raise UpstreamError(status, data)
        return data

    def get(self, key: str, default=None):
        with self.store.transaction() as tx:
            return tx.get("state", key, default)

    def put(self, key: str, value):
        with self.store.transaction() as tx:
            tx.put("state", key, value)

    def id_for(self, upstream_id: str) -> int:
        with self.store.transaction() as tx:
            existing = tx.get("reverse_ids", upstream_id)
            if existing is not None:
                return existing
            value = tx.next_id("legacy_ids")
            tx.put("reverse_ids", upstream_id, value)
            tx.put("ids", value, upstream_id)
            return value

    def upstream_id(self, legacy_id: int):
        with self.store.transaction() as tx:
            return tx.get("ids", legacy_id)

    def mappings(self):
        with self.store.transaction() as tx:
            return tx.all("ids")

    @staticmethod
    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def close(self):
        self.http.close()
        self.store.close()
