"""
Central .env loader
=====================
python-dotenv was listed in requirements.txt but never actually called
anywhere in this codebase -- every script here only ever picked up
environment variables that were manually exported in the shell before
running it. A real user following the setup instructions (cp
.env.example .env, fill in a real key, then just run
`uvicorn app.api.main:app`) would get NOTHING from that file:
os.environ.get(...) calls throughout the app would silently fall back
to their default (usually "" or mock mode), with no error explaining
why a real API key in .env isn't being picked up.

Every real entry point in this codebase (the FastAPI app itself, and
every standalone script under app/demo, app/eval, app/loadtest,
app/fuzz) must call load_env() as its first import-time action, before
importing any other app module that reads os.environ.get(...).
"""

from __future__ import annotations
from pathlib import Path
from dotenv import load_dotenv

# backend/.env, resolved relative to this file's own location -- not
# the current working directory. CWD varies depending on how a script
# is invoked (uvicorn vs. `python -m app.eval.eval_harness` vs. pytest
# vs. a CI runner that cd's somewhere first); resolving relative to
# __file__ works the same regardless of where you run it from.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_loaded = False


def load_env() -> None:
    """Idempotent -- safe to call from multiple entry points/imports
    without re-parsing the file every time. override=False: a variable
    already set in the real environment (Docker's `environment:`
    block, CI secrets, a manual export) always wins over .env -- a
    local dev file should never silently clobber a deliberately set
    production value.
    """
    global _loaded
    if _loaded:
        return
    load_dotenv(dotenv_path=_ENV_PATH, override=False)
    _loaded = True
