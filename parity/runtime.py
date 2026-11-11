"""Engine-owned plugin ABI. No commerce mappings or workflow policy live here."""

import fcntl
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from parity.storage import Store


class UpstreamError(Exception):
    def __init__(self, status, data, headers=None):
        super().__init__(f"upstream HTTP {status}")
        self.status, self.data = status, data
        self.headers = headers or {}


@dataclass(frozen=True)
class Response:
    status: int
    body: object = None
    headers: dict | None = None


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

    def __init__(
        self,
        upstream: str,
        database: str,
        trace: str | None = None,
        *,
        upstream_socket=None,
        trace_bodies=True,
    ):
        self.store = Store(database)
        self.base = Path(database).resolve().parent
        transport = httpx.HTTPTransport(uds=upstream_socket) if upstream_socket else None
        self.http = httpx.Client(base_url=upstream, timeout=3, trust_env=False, transport=transport)
        self.trace_bodies = trace_bodies
        self.trace = Path(trace) if trace else self.base / "http.jsonl"
        self.thread_lock = threading.RLock()
        self.local = threading.local()

    @contextmanager
    def request_scope(self):
        self.local.calls = 0
        self.local.trace_id = uuid.uuid4().hex
        self.local.request_active = True
        try:
            yield
        finally:
            self.local.request_active = False

    @contextmanager
    def lock(self, _key="global"):
        # A global lock avoids checkout/cancel/list races. Throughput is intentionally bounded.
        with self.thread_lock:
            if getattr(self.local, "locked", False):
                yield
                return
            with (self.base / "adapter.lock").open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                self.local.locked = True
                try:
                    if not getattr(self.local, "request_active", False):
                        self.local.calls = 0
                        self.local.trace_id = uuid.uuid4().hex
                    yield
                finally:
                    self.local.locked = False
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def call(self, method: str, path: str, body=None, headers=None, query=None):
        response = self.request(method, path, body=body, headers=headers, query=query)
        if response.status >= 400:
            raise UpstreamError(response.status, response.body, response.headers)
        return response.body

    def request(self, method: str, path: str, body=None, headers=None, query=None):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("only relative upstream paths are allowed")
        self.local.calls = getattr(self.local, "calls", 0) + 1
        if self.local.calls > 80:
            raise UpstreamError(503, {"problem": {"kind": "CALL_BUDGET"}})
        start = time.perf_counter()
        response_headers = {}
        try:
            response = self.http.request(method, path, json=body, headers=headers, params=query)
            status = response.status_code
            response_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                not in {"connection", "transfer-encoding", "content-length", "content-encoding"}
            }
            if not response.content:
                data = None
            else:
                try:
                    data = response.json()
                except ValueError:
                    data = response.text
        except httpx.HTTPError:
            data, status = {"problem": {"kind": "TEMPORARY"}}, 503
        event = {
            "trace_id": getattr(self.local, "trace_id", None),
            "call_sequence": self.local.calls,
            "method": method,
            "path": path,
            "body": redact(body) if self.trace_bodies else None,
            "status": status,
            "response": redact(data) if self.trace_bodies else None,
            "ms": round((time.perf_counter() - start) * 1000, 3),
        }
        with self.trace.open("a") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
        return Response(status, data, response_headers)

    @staticmethod
    def respond(status, body=None, headers=None):
        return Response(status, body, headers)

    def record_request(self, method, path, body, query, headers):
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        safe_headers = {
            k: v
            for k, v in headers.items()
            if k.lower() in {"idempotency-key", "content-type", "if-match", "if-none-match"}
        }
        request = {
            "method": method,
            "path": path,
            "body": body,
            "query": query,
            "headers": safe_headers,
        }
        key = self.digest(request)
        self.record_result(key, "pending", request=request, stage="running")
        return key

    def record_result(self, key, status, *, request=None, stage=None, http_status=None):
        if key is None:
            return
        with self.store.transaction() as tx:
            value = tx.get("workflows", key, {})
            value.update(
                id=key,
                status=status,
                stage=stage or status,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            if request is not None:
                value["request"] = request
            if http_status is not None:
                value["http_status"] = http_status
            tx.put("workflows", key, value)

    def import_ids(self, mappings):
        with self.store.transaction() as tx:
            normalized = {int(key): value for key, value in mappings.items()}
            if (
                len(normalized) != len(mappings)
                or any(
                    key <= 0 or not isinstance(value, str) or not value
                    for key, value in normalized.items()
                )
                or len(set(normalized.values())) != len(normalized)
            ):
                raise ValueError(
                    "ID mappings require unique positive integers and nonempty upstream IDs"
                )
            for key, value in normalized.items():
                if tx.get("ids", key, value) != value or tx.get("reverse_ids", value, key) != key:
                    raise ValueError("ID mapping conflicts with existing correspondence")
                tx.put("ids", key, value)
                tx.put("reverse_ids", value, key)
            tx.put(
                "counters",
                "legacy_ids",
                max(tx.get("counters", "legacy_ids", 0), max(normalized, default=0)),
            )

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
