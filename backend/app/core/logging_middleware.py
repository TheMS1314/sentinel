"""
Structured Logging Middleware
================================
Logs one structured (JSON) line per request: a request_id (generated if
the caller didn't supply one), method, path, status code, duration, and
this replica's instance ID. Also echoes the request_id back as a
response header, so a caller (or a human debugging a support ticket)
can correlate "this specific API call" across logs even across
multiple backend replicas.

Why this matters for THIS system specifically: Sentinel's whole premise
is auditability. The transaction-level audit trail (audit_logger.py)
answers "why was this payment decision made" -- this answers the
adjacent but different question "what did the API actually do, when,
and how long did it take", which matters for debugging and for being
able to say something concrete about production behavior rather than
just "it works."
"""

from __future__ import annotations
import time
import uuid
import json
import logging
import os
import re
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("sentinel.access")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", supplied)
            else f"req_{uuid.uuid4().hex}"
        )
        request.state.request_id = request_id
        start = time.perf_counter()

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id

        log_entry = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path[:256],
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "instance_id": os.environ.get("SENTINEL_INSTANCE_ID", "unknown"),
            "client_ip": request.client.host if request.client else None,
        }
        logger.info(json.dumps(log_entry))

        return response
