"""Copied with runtime/storage into a restricted, credential-free temporary directory."""

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from parity.runtime import Context


def main():
    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "config.json").read_text())
    spec = importlib.util.spec_from_file_location("migration", root / "adapter.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("adapter module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = Context(config["upstream"], str(root / "state" / "adapter.db"))

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

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def route(path: str, request: Request):
        raw = await request.body()
        if len(raw) > 65536:
            return JSONResponse({"error": "REQUEST_TOO_LARGE"}, status_code=413)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return JSONResponse({"error": "INVALID"}, status_code=422)
        status, data = await run_in_threadpool(
            module.handle,
            ctx,
            request.method,
            "/" + path,
            body,
            dict(request.query_params),
            dict(request.headers),
        )
        return JSONResponse(data, status_code=status)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=config["port"],
        log_level="warning",
        access_log=False,
        timeout_keep_alive=1,
    )


if __name__ == "__main__":
    main()
