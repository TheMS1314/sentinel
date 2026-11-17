"""Enforce bounded body buffering and admission before parsing application input."""

import json
import asyncio
from app.core.settings import integer


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app
        self.max_body = integer(
            "SENTINEL_MAX_BODY_BYTES", 524288, minimum=65536, maximum=2097152
        )
        self.max_active = integer("SENTINEL_HTTP_CONCURRENCY", 32, maximum=256)
        self.active = 0

    async def reject(self, send, status, message):
        data = json.dumps(
            {"error": {"message": message, "status_code": status}}
        ).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(data)).encode()),
        ]
        if status == 503:
            headers.append((b"retry-after", b"1"))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": data})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        probe = scope.get("path") in ("/health/live", "/health/ready", "/health")
        if not probe and self.active >= self.max_active:
            return await self.reject(
                send, 503, "Server capacity reached; retry with the same request ID"
            )
        limit = 65536 if scope.get("path") == "/payments/webhook" else self.max_body
        lengths = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        try:
            if len(lengths) > 1 or (lengths and int(lengths[0]) < 0):
                return await self.reject(send, 400, "Invalid content length")
            if lengths and int(lengths[0]) > limit:
                return await self.reject(send, 413, "Request body too large")
        except ValueError:
            return await self.reject(send, 400, "Invalid content length")
        if not probe:
            self.active += 1  # Single event-loop operation; no await before increment.
        try:
            chunks, size = [], 0
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > limit:
                            return await self.reject(
                                send, 413, "Request body too large"
                            )
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await self.reject(send, 408, "Request body deadline exceeded")
            body = b"".join(chunks)
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
        finally:
            if not probe:
                self.active -= 1
