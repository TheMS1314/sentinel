"""Redacted software provenance for offline verification artifacts."""

import os
from pathlib import Path
import subprocess


def revision():
    supplied = os.environ.get("GITHUB_SHA") or os.environ.get("SENTINEL_BUILD_SHA")
    if supplied:
        return dict(commit=supplied, dirty=None)
    try:
        root = Path(__file__).resolve().parents[3]
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout.strip()
        changed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        ).stdout
        return dict(commit=sha, dirty=bool(changed))
    except (OSError, subprocess.SubprocessError):
        return dict(commit=None, dirty=None)
