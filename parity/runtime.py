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

from parity.http_values import (
    DECODED_BODY_HEADERS,
    connection_tokens,
    finite_float,
    is_json_media_type,
)
from parity.storage import Store

LEGACY_REPLAY_HEADERS = {"content-type", "idempotency-key", "if-match", "if-none-match"}
REPLAY_HEADERS = LEGACY_REPLAY_HEADERS | {
    "accept",
    "accept-encoding",
    "accept-language",
    "user-agent",
}
TRANSPORT_HEADERS = {"host", "connection", "content-length", "transfer-encoding"}


class UpstreamError(Exception):
    def __init__(self, status, data, headers=None):
        super().__init__(f"upstream HTTP {status}")
        self.status, self.data = status, data
        self.headers = headers or {}


class ResponseHeaders(dict):
    """Preserve HTTP field lines while exposing the original dictionary interface."""

    def __init__(self, fields):
        headers = httpx.Headers(fields, encoding="latin-1")
        super().__init__(headers.items())
        self._original = dict(self)
        self._fields = [(name.lower(), value) for name, value in headers.raw]

    def field_lines(self):
        current = {name.lower(): value for name, value in self.items()}
        unchanged = {name for name, value in current.items() if value == self._original.get(name)}
        return [(name, value) for name, value in self._fields if name.decode() in unchanged] + [
            (name.encode("ascii"), value.encode("latin-1"))
            for name, value in current.items()
            if name not in unchanged
        ]


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
        response_headers: dict = {}
        try:
            request = httpx.Request(
                method, self.http.base_url.join(path), json=body, headers=headers, params=query
            )
            response = self.http.send(request)
            status = response.status_code
            excluded = DECODED_BODY_HEADERS | connection_tokens(response.headers)
            response_headers = ResponseHeaders(
                [
                    (name, value)
                    for name, value in response.headers.raw
                    if name.decode().lower() not in excluded
                ]
            )
            if not response.content:
                data = None
                if status not in (204, 304):
                    response_headers["content-length"] = (
                        response.headers.get("content-length", "0")
                        if method.upper() == "HEAD"
                        else "0"
                    )
            elif is_json_media_type(response.headers.get("content-type", "")):
                try:
                    data = response.json(parse_float=finite_float, parse_constant=finite_float)
                except ValueError:
                    data, status = {"problem": {"kind": "INVALID_UPSTREAM_JSON"}}, 502
                    response_headers = {}
            else:
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
        headers = {name.lower(): value for name, value in headers.items()}
        request = {
            "method": method,
            "path": path,
            "body": body,
            "query": query,
            "headers": {name: value for name, value in headers.items() if name in REPLAY_HEADERS},
        }
        key, attempt = self.digest(request), uuid.uuid4().hex
        with self.store.transaction() as tx:
            legacy_headers = {
                name: value for name, value in headers.items() if name in LEGACY_REPLAY_HEADERS
            }
            if legacy_headers != request["headers"]:
                legacy_key = self.digest({**request, "headers": legacy_headers})
                prior = tx.get("workflows", legacy_key)
                if prior and ("unreplayed_headers" not in prior or prior["request"] == request):
                    key = legacy_key
            tx.put(
                "workflows",
                key,
                {
                    "id": key,
                    "attempt": attempt,
                    "request": request,
                    "unreplayed_headers": sorted(
                        headers.keys() - REPLAY_HEADERS - TRANSPORT_HEADERS
                    ),
                    "status": "pending",
                    "stage": "running",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        return key, attempt

    def record_result(self, attempt_ref, status, *, stage=None, http_status=None):
        if attempt_ref is None:
            return
        key, attempt = attempt_ref
        with self.store.transaction() as tx:
            value = tx.get("workflows", key)
            if value is None or value.get("attempt") != attempt:
                return
            value.update(
                status=status,
                stage=stage or status,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            if http_status is not None:
                value["http_status"] = http_status
            tx.put("workflows", key, value)

    def import_ids(self, mappings):
        if not isinstance(mappings, dict):
            raise ValueError("ID mappings must be a JSON object")
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
