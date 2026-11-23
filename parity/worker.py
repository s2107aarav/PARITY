"""Serve an adapter from its restricted, credential-free working directory."""

import importlib.util
import json
import resource
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response as HTTPResponse
from starlette.concurrency import run_in_threadpool

from parity.http_values import (
    DECODED_BODY_HEADERS,
    connection_tokens,
    finite_float,
    is_json_media_type,
)
from parity.runtime import Context, Response, ResponseHeaders


def render_response(result, method):
    if isinstance(result, Response):
        status, data = result.status, result.body
        fields = result.headers
        if isinstance(fields, ResponseHeaders):
            fields = fields.field_lines()
        headers = httpx.Headers(fields, encoding="latin-1")
    else:
        status, data = result
        headers = httpx.Headers()
    if type(status) is not int or not 200 <= status <= 599:
        raise ValueError("adapter must return a final HTTP status")
    media = headers.get("content-type", "")
    no_payload = status in (204, 205, 304)
    if (
        no_payload
        or method == "HEAD"
        or (
            isinstance(result, Response)
            and data is None
            and (not media or headers.get("content-length") == "0")
        )
    ):
        response = HTTPResponse(status_code=status)
    elif isinstance(result, Response) and isinstance(data, str) and not is_json_media_type(media):
        encoding = httpx.Response(status, headers=headers).encoding or "utf-8"
        response = HTTPResponse(data.encode(encoding), status_code=status, media_type="text/plain")
    else:
        response = JSONResponse(data, status_code=status)

    excluded = DECODED_BODY_HEADERS | connection_tokens(headers)
    raw = [
        (name.lower(), value)
        for name, value in headers.raw
        if name.decode().lower() not in excluded
    ]
    if method == "HEAD" and not no_payload:
        length = headers.get("content-length", "")
        if length.isascii() and length.isdigit():
            raw.append((b"content-length", length.encode("ascii")))
    preserved = {name for name, _ in raw}
    response.raw_headers = [
        field for field in response.raw_headers if field[0] not in preserved
    ] + raw
    return response


def apply_limits(policy):
    limits = {
        resource.RLIMIT_CPU: policy["cpu_seconds"],
        resource.RLIMIT_NOFILE: policy["descriptors"],
        resource.RLIMIT_FSIZE: policy["file_bytes"],
    }
    if sys.platform.startswith("linux"):
        limits[resource.RLIMIT_AS] = policy["linux_address_space_bytes"]
    for kind, value in limits.items():
        resource.setrlimit(kind, (value, value))


def main():
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "config.json").read_text())
    apply_limits(config["limits"])
    spec = importlib.util.spec_from_file_location("migration", root / "adapter.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("adapter module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = Context(
        config["upstream"],
        str(root / "state" / "adapter.db"),
        upstream_socket=config.get("upstream_socket"),
        trace_bodies=config.get("trace_bodies", True),
    )

    @asynccontextmanager
    async def lifespan(_app):
        # Linux pthread stacks reserve virtual memory even when RSS is small.
        # Eight threads cover the six-request retry control without AnyIO's default 40.
        anyio.to_thread.current_default_thread_limiter().total_tokens = config["limits"][
            "worker_threads"
        ]
        try:
            yield
        finally:
            ctx.close()

    app = FastAPI(title="PARITY adapter", docs_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/__parity_health")
    def health():
        return {"ready": True}

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    )
    async def route(path: str, request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
        try:
            body = (
                json.loads(raw, parse_float=finite_float, parse_constant=finite_float)
                if raw
                else None
            )
        except ValueError:
            return JSONResponse({"error": "INVALID"}, status_code=422)
        query: dict = dict(request.query_params)
        for key in query:
            values = request.query_params.getlist(key)
            if len(values) > 1:
                query[key] = values
        headers = dict(request.headers)
        upstream_path = request.scope["raw_path"].decode("ascii")
        attempt = await run_in_threadpool(
            ctx.record_request, request.method, upstream_path, body, query, headers
        )
        try:

            def invoke():
                with ctx.request_scope():
                    return module.handle(ctx, request.method, upstream_path, body, query, headers)

            response = render_response(await run_in_threadpool(invoke), request.method)
            status = response.status_code
            await run_in_threadpool(
                ctx.record_result,
                attempt,
                "pending" if status >= 500 else "completed" if status < 400 else "failed",
                http_status=status,
            )
            return response
        except Exception:
            await run_in_threadpool(
                ctx.record_result, attempt, "pending", stage="adapter_error", http_status=500
            )
            return JSONResponse({"error": "ADAPTER_ERROR"}, status_code=500)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=config["port"],
        uds=config.get("listen_socket"),
        log_level="warning",
        access_log=False,
        timeout_keep_alive=1,
    )


if __name__ == "__main__":
    main()
