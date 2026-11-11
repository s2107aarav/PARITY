"""Copied with runtime/storage into a restricted, credential-free temporary directory."""

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response as HTTPResponse
from starlette.concurrency import run_in_threadpool

from parity.runtime import Context, Response


def main():
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "config.json").read_text())
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
        anyio.to_thread.current_default_thread_limiter().total_tokens = 8
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
            body = json.loads(raw) if raw else None
        except ValueError:
            return JSONResponse({"error": "INVALID"}, status_code=422)
        query: dict = dict(request.query_params)
        for key in query:
            values = request.query_params.getlist(key)
            if len(values) > 1:
                query[key] = values
        headers = dict(request.headers)
        upstream_path = request.scope["raw_path"].decode("ascii")
        key = await run_in_threadpool(
            ctx.record_request, request.method, upstream_path, body, query, headers
        )
        try:

            def invoke():
                with ctx.request_scope():
                    return module.handle(ctx, request.method, upstream_path, body, query, headers)

            result = await run_in_threadpool(invoke)
            if isinstance(result, Response):
                status, data = result.status, result.body
                response_headers = {
                    key.lower(): value for key, value in (result.headers or {}).items()
                }
            else:
                status, data = result
                response_headers = None
            await run_in_threadpool(
                ctx.record_result,
                key,
                "pending" if status >= 500 else "completed" if status < 400 else "failed",
                http_status=status,
            )
            if status in (204, 304) or request.method == "HEAD":
                return HTTPResponse(status_code=status, headers=response_headers)
            if (
                isinstance(result, Response)
                and isinstance(data, str)
                and "json" not in (response_headers or {}).get("content-type", "")
            ):
                return HTTPResponse(
                    data, status_code=status, headers=response_headers, media_type="text/plain"
                )
            return JSONResponse(data, status_code=status, headers=response_headers)
        except Exception:
            await run_in_threadpool(
                ctx.record_result, key, "pending", stage="adapter_error", http_status=500
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
