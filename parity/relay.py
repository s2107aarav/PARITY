"""Trusted HTTP relay: upstream credentials never enter generated workers."""

from contextlib import asynccontextmanager, closing

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
        blocked = (
            (HOP_HEADERS - {"content-encoding"})
            | {name.lower() for name in (headers or {})}
            | {name.strip().lower() for name in request.headers.get("connection", "").split(",")}
        )
        incoming = [
            (key, value) for key, value in request.headers.items() if key.lower() not in blocked
        ]
        incoming.extend((headers or {}).items())
        upstream_path = request.scope["raw_path"].decode("ascii")
        if not upstream_path.startswith("/") or upstream_path.startswith("//"):
            return Response("Invalid upstream path", status_code=400)
        try:
            from starlette.concurrency import run_in_threadpool

            def send():
                outgoing = httpx.Request(
                    request.method,
                    client.base_url.join(upstream_path),
                    params=list(request.query_params.multi_items()),
                    content=bytes(body),
                    headers=incoming,
                )
                with closing(client.send(outgoing, stream=True)) as result:
                    content = bytearray()
                    for chunk in result.iter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            return Response("Upstream response exceeds 1 MiB", status_code=502)
                    excluded = HOP_HEADERS | {
                        name.strip().lower()
                        for name in result.headers.get("connection", "").split(",")
                    }
                    response = Response(bytes(content), status_code=result.status_code)
                    response.raw_headers.extend(
                        (key, value)
                        for key, value in result.headers.raw
                        if key.decode("ascii").lower() not in excluded
                    )
                    return response

            return await run_in_threadpool(send)
        except httpx.HTTPError:
            return Response("Upstream transport failure", status_code=503)

    return app
