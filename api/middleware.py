"""Bound the actual body (including chunked requests) and request lifetime."""

import asyncio

from starlette.responses import JSONResponse


class ResourceLimitsMiddleware:
    def __init__(self, app, max_body_bytes: int, timeout_seconds: float):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        async def bounded_request():
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > self.max_body_bytes:
                    return await JSONResponse(status_code=413, content={"detail": {
                        "code": "body_too_large", "message": "El cuerpo supera 16 KiB; envíe identificadores y pesos, no rásteres ni datasets."
                    }})(scope, receive, tracked_send)
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, tracked_send)

        try:
            await asyncio.wait_for(bounded_request(), timeout=self.timeout_seconds)
        except TimeoutError:
            if not started:
                await JSONResponse(status_code=503, content={"detail": {
                    "code": "request_timeout", "message": "Se agotó el tiempo máximo de la consulta."
                }})(scope, receive, send)
