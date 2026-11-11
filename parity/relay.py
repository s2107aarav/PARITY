"""Trusted HTTP relay: upstream credentials never enter generated workers."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

HOP_HEADERS = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "content-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


def relay_app(target, *, headers=None, socket_path=None, timeout=10, allow_writes=True):
    client = httpx.Client(
        base_url=target,
        headers=headers or {},
        trust_env=False,
        timeout=timeout,
        transport=httpx.HTTPTransport(uds=str(socket_path)) if socket_path else None,
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            client.close()

    app = FastAPI(docs_url=None, openapi_url=None, lifespan=lifespan)

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    )
    async def forward(path: str, request: Request):
        if not allow_writes and request.method not in {"GET", "HEAD", "OPTIONS"}:
            return Response("Writes are disabled in parity.json", status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                return Response(status_code=413)
        incoming = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_HEADERS | {name.lower() for name in (headers or {})}
        }
        incoming.update(headers or {})
        upstream_path = request.scope["raw_path"].decode("ascii")
        if not upstream_path.startswith("/") or upstream_path.startswith("//"):
            return Response("Invalid upstream path", status_code=400)
        try:
            from starlette.concurrency import run_in_threadpool

            def send():
                with client.stream(
                    request.method,
                    upstream_path,
                    params=list(request.query_params.multi_items()),
                    content=bytes(body),
                    headers=incoming,
                ) as result:
                    content = bytearray()
                    for chunk in result.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            return Response("Upstream response exceeds 1 MiB", status_code=502)
                    safe_headers = {
                        key: value
                        for key, value in result.headers.items()
                        if key.lower() not in HOP_HEADERS
                    }
                    return Response(
                        bytes(content), status_code=result.status_code, headers=safe_headers
                    )

            return await run_in_threadpool(send)
        except httpx.HTTPError:
            return Response("Upstream transport failure", status_code=503)

    return app
