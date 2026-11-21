"""Create local development credentials once; never print or overwrite secrets."""

from pathlib import Path
import json
import secrets

root = Path(__file__).resolve().parents[1]
backend = root / "backend/.env"
compose = root / ".env"
if backend.exists() or compose.exists():
    raise SystemExit(
        "An environment file exists. Review it manually; nothing overwritten."
    )
password = secrets.token_urlsafe(32)
key = secrets.token_urlsafe(32)
compose.write_text(
    f"POSTGRES_PASSWORD={password}\nSENTINEL_RUNTIME_DB_PASSWORD={secrets.token_urlsafe(32)}\n"
)
backend.write_text(
    "SENTINEL_ENV=development\nLLM_PROVIDER=openrouter\nOPENROUTER_API_KEY=\n"
    "RAZORPAY_KEY_ID=\nRAZORPAY_KEY_SECRET=\n"
    "SENTINEL_ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://localhost:5173,http://127.0.0.1:5173\n"
    "SENTINEL_CREDENTIALS="
    + json.dumps([dict(key=key, subject="local-admin", role="admin")])
    + "\n"
)
backend.chmod(0o600)
compose.chmod(0o600)
print(
    "Created environment files. The local dashboard credential is in backend/.env; keep it private."
)
