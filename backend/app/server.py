"""Bounded server entry point; trust forwarded headers only from configured proxies."""

import os
import uvicorn
from app.core.env import load_env
from app.core.settings import integer

if __name__ == "__main__":
    load_env()
    proxies = os.environ.get("SENTINEL_TRUSTED_PROXY_IPS", "127.0.0.1")
    if "*" in proxies:
        raise SystemExit(
            "Configure trusted proxy addresses explicitly; wildcard trust is disabled"
        )
    uvicorn.run(
        "app.api.main:app",
        host="0.0.0.0",
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips=proxies,
        limit_concurrency=integer("SENTINEL_SERVER_CONCURRENCY", 64, maximum=512),
        timeout_keep_alive=5,
        timeout_graceful_shutdown=45,
        backlog=256,
        server_header=False,
        access_log=False,
    )
