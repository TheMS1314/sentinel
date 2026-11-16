import os
import uuid
import pytest
from starlette.requests import Request
from fastapi import HTTPException
from app.core.auth import Principal


def test_redis_quota_is_shared_and_retry_header_preserved(monkeypatch):
    import asyncio
    import redis
    import app.core.rate_limit as module

    try:
        redis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            socket_connect_timeout=1,
            socket_timeout=1,
        ).ping()
    except Exception:
        pytest.skip("isolated Redis unavailable")
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setattr(module, "RATE_LIMIT_REQUESTS", 2)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )
    request.state.principal = Principal("quota_" + uuid.uuid4().hex, "admin")

    async def run():
        await module.enforce_rate_limit(request)
        await module.enforce_rate_limit(request)
        with pytest.raises(HTTPException) as error:
            await module.enforce_rate_limit(request)
        assert error.value.status_code == 429
        assert int(error.value.headers["Retry-After"]) > 0

    asyncio.run(run())
