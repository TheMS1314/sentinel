"""A misspelled production profile must not silently become a development server."""

import asyncio
from unittest.mock import patch
import pytest
from app.core.env import load_env
from app.api.main import app, lifespan


@pytest.mark.parametrize(
    "profile", ["Production", "produciton", "staging", "evaluation", "integration_test"]
)
def test_server_rejects_unknown_or_offline_profiles_before_authority_io(
    monkeypatch, profile
):
    monkeypatch.setenv("SENTINEL_ENV", profile)

    async def start():
        async with lifespan(app):
            raise AssertionError("Invalid profile served requests")

    with patch("app.api.main.validate_authority") as authority:
        with pytest.raises(RuntimeError):
            asyncio.run(start())
        authority.assert_not_called()


def test_repeated_environment_loading_revalidates_the_profile(monkeypatch):
    monkeypatch.setenv("SENTINEL_ENV", "test")
    load_env()
    monkeypatch.setenv("SENTINEL_ENV", "produciton")
    with pytest.raises(RuntimeError, match="Unknown SENTINEL_ENV"):
        load_env()
