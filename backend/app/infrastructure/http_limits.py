"""ASGI request-body cap applied before FastAPI parses multipart/JSON bodies.

Count chunks as they arrive without collecting or copying them. Content-Length
is only a preflight optimization: chunked bodies and lying lengths are still
bounded. Multipart endpoints parse the body before executing their handler, so
an exceeded body cannot submit an ingestion job. Reverse-proxy limits remain an
additional bound on connections, deadlines and bytes buffered by the server.
"""

from __future__ import annotations

import json
import re

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _BodyRejected(HTTPException):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int = 51 * 1024 * 1024) -> None:
        if not isinstance(max_body_bytes, int) or max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def reject(status: int, detail: str) -> None:
            body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
            ]})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        headers = scope.get("headers", [])
        lengths = [value.strip() for key, value in headers if key.lower() == b"content-length"]
        has_transfer_encoding = any(key.lower() == b"transfer-encoding" for key, _ in headers)
        if len(lengths) > 1 or (lengths and has_transfer_encoding):
            await reject(400, "Cabeceras de longitud del cuerpo ambiguas.")
            return
        if lengths:
            if not re.fullmatch(rb"[0-9]{1,20}", lengths[0]):
                await reject(400, "Content-Length inválido.")
                return
            if int(lengths[0]) > self.max_body_bytes:
                await reject(413, "El cuerpo de la solicitud excede el límite permitido.")
                return

        total = 0
        rejected: _BodyRejected | None = None
        response_started = False
        rejection_sent = False

        async def limited_receive() -> Message:
            nonlocal total, rejected
            if rejected is not None:
                raise rejected
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    rejected = _BodyRejected(413, "El cuerpo de la solicitud excede el límite permitido.")
                    raise rejected
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started, rejection_sent
            # Some body parsers catch HTTPException and format their own error.
            # Keep the rejection status stable even if they map it to 400/500.
            if rejected is not None:
                if not rejection_sent and not response_started:
                    await reject(rejected.status_code, str(rejected.detail))
                    rejection_sent = True
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if rejected is None or response_started:
                raise
            if not rejection_sent:
                await reject(rejected.status_code, str(rejected.detail))
                rejection_sent = True
        if rejected is not None and not rejection_sent and not response_started:
            await reject(rejected.status_code, str(rejected.detail))
