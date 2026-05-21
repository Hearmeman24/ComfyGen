"""Tests for the worker-level /volume_info command.

The /volume_info command must return total/used/free bytes for the network
volume (`/runpod-volume`) so BlockFlow's preset installer can pre-check disk
space before starting a download. Must be fast — a single `os.statvfs` syscall,
no GPU work, no model loading.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


def _statvfs_result(*, f_blocks: int, f_bavail: int, f_frsize: int) -> SimpleNamespace:
    """Build a stand-in for the os.statvfs_result returned by os.statvfs.

    We only populate the three fields the handler reads. The real object has
    more fields, but accessing only these mirrors what the handler does.
    """
    return SimpleNamespace(f_blocks=f_blocks, f_bavail=f_bavail, f_frsize=f_frsize)


def test_volume_info_returns_expected_shape(dispatch_command, mocker):
    # 1 TiB total, 400 GiB free, 4096-byte fragments
    f_frsize = 4096
    f_blocks = (1 << 40) // f_frsize
    f_bavail = (400 * (1 << 30)) // f_frsize
    mocker.patch(
        "os.statvfs",
        return_value=_statvfs_result(f_blocks=f_blocks, f_bavail=f_bavail, f_frsize=f_frsize),
    )

    result = dispatch_command({"command": "volume_info"})

    assert result["ok"] is True
    assert result["path"] == "/runpod-volume"
    assert result["size_bytes"] == f_blocks * f_frsize
    assert result["free_bytes"] == f_bavail * f_frsize
    assert result["used_bytes"] == result["size_bytes"] - result["free_bytes"]


def test_volume_info_queries_runpod_volume_path(dispatch_command, mocker):
    spy = mocker.patch(
        "os.statvfs",
        return_value=_statvfs_result(f_blocks=1000, f_bavail=500, f_frsize=4096),
    )
    dispatch_command({"command": "volume_info"})
    spy.assert_called_once_with("/runpod-volume")


def test_volume_info_byte_values_are_positive_ints(dispatch_command, mocker):
    mocker.patch(
        "os.statvfs",
        return_value=_statvfs_result(f_blocks=2048, f_bavail=1024, f_frsize=4096),
    )
    result = dispatch_command({"command": "volume_info"})
    for key in ("size_bytes", "used_bytes", "free_bytes"):
        assert isinstance(result[key], int), f"{key} must be int, got {type(result[key])}"
        assert result[key] >= 0, f"{key} must be non-negative, got {result[key]}"
    assert result["size_bytes"] > 0


def test_volume_info_used_plus_free_within_size(dispatch_command, mocker):
    mocker.patch(
        "os.statvfs",
        return_value=_statvfs_result(f_blocks=10000, f_bavail=3000, f_frsize=4096),
    )
    result = dispatch_command({"command": "volume_info"})
    assert result["used_bytes"] + result["free_bytes"] <= result["size_bytes"]


def test_volume_info_full_disk(dispatch_command, mocker):
    # Zero free space — used == size
    mocker.patch(
        "os.statvfs",
        return_value=_statvfs_result(f_blocks=1000, f_bavail=0, f_frsize=4096),
    )
    result = dispatch_command({"command": "volume_info"})
    assert result["free_bytes"] == 0
    assert result["used_bytes"] == result["size_bytes"]


def test_volume_info_missing_path_returns_error(dispatch_command, mocker):
    mocker.patch("os.statvfs", side_effect=FileNotFoundError("/runpod-volume"))
    result = dispatch_command({"command": "volume_info"})
    assert result["ok"] is False
    assert "error" in result
    assert isinstance(result["error"], str)
    assert result["error"]  # non-empty


def test_volume_info_permission_error_returns_error(dispatch_command, mocker):
    mocker.patch("os.statvfs", side_effect=PermissionError("denied"))
    result = dispatch_command({"command": "volume_info"})
    assert result["ok"] is False
    assert "error" in result


def test_volume_info_ignores_extra_input_fields(dispatch_command, mocker):
    mocker.patch(
        "os.statvfs",
        return_value=_statvfs_result(f_blocks=1000, f_bavail=500, f_frsize=4096),
    )
    result = dispatch_command(
        {"command": "volume_info", "unexpected": "junk", "path": "/etc"}
    )
    # Path is hardcoded — extra input does not redirect the query
    assert result["ok"] is True
    assert result["path"] == "/runpod-volume"
