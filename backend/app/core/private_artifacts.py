"""Atomic owner-only JSON output for reports containing private request data."""

import json
import os
from pathlib import Path
import tempfile


def write_private_json(path, value):
    """Never expose a partially written report or follow a final-path symlink."""
    destination = Path(path)
    # Encode before opening anything; invalid data must preserve the old report.
    raw = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sentinel-report-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
