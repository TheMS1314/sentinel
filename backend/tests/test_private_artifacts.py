"""Private report writes are atomic even under failure and hostile output symlinks."""

import json
import os
from unittest.mock import patch
import pytest
from app.core.private_artifacts import write_private_json


def test_report_is_private_before_any_bytes_are_written(tmp_path):
    original = os.fdopen
    modes = []

    def inspect(fd, *args, **kwargs):
        modes.append(os.fstat(fd).st_mode & 0o777)
        return original(fd, *args, **kwargs)

    with patch("app.core.private_artifacts.os.fdopen", side_effect=inspect):
        write_private_json(tmp_path / "report.json", {"private": "case text"})
    assert modes == [0o600]
    assert (tmp_path / "report.json").stat().st_mode & 0o777 == 0o600


def test_failed_replacement_keeps_original_and_cleans_temporary(tmp_path):
    path = tmp_path / "report.json"
    write_private_json(path, {"original": True})
    with patch(
        "app.core.private_artifacts.os.replace", side_effect=OSError("disk failure")
    ):
        with pytest.raises(OSError):
            write_private_json(path, {"replacement": True})
    assert json.loads(path.read_text()) == {"original": True}
    assert set(tmp_path.iterdir()) == {path}


def test_symlink_output_does_not_overwrite_its_target(tmp_path):
    target = tmp_path / "unrelated.txt"
    target.write_text("retain this")
    output = tmp_path / "report.json"
    output.symlink_to(target)
    write_private_json(output, {"new": "report"})
    assert target.read_text() == "retain this"
    assert not output.is_symlink()
    assert json.loads(output.read_text()) == {"new": "report"}


def test_invalid_json_does_not_truncate_existing_report(tmp_path):
    output = tmp_path / "report.json"
    output.write_text("old")
    with pytest.raises(ValueError):
        write_private_json(output, {"score": float("nan")})
    assert output.read_text() == "old"
