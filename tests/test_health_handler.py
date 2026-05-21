"""Tests for the worker-level /health command.

The /health command must return `{ok: true, version: "X.Y.Z"}` quickly (no model
loading, no GPU work). BlockFlow's "attach existing endpoint" flow calls this to
verify reachability and to gate preset compatibility against a declared
`comfygen_min_version`. The runtime version MUST track pyproject.toml — drift
between the two would silently break preset version-gating.
"""

from __future__ import annotations

import re

import pytest


def test_health_returns_ok_and_version(dispatch_command, pyproject_version):
    result = dispatch_command({"command": "health"})
    assert result == {"ok": True, "version": pyproject_version}


def test_health_version_is_semver(dispatch_command):
    result = dispatch_command({"command": "health"})
    assert isinstance(result["version"], str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", result["version"])


def test_unknown_command_raises(dispatch_command):
    with pytest.raises(ValueError, match="unknown command"):
        dispatch_command({"command": "bogus"})


def test_missing_command_field_raises(dispatch_command):
    with pytest.raises(ValueError, match="unknown command"):
        dispatch_command({})


def test_health_handler_module_matches_pyproject(pyproject_version):
    """Guards against version drift: serverless-runtime ships separately from the
    `comfy-gen` package, so the version constant in health_handler is maintained
    by hand. If this test fails, bump health_handler.VERSION to match pyproject.toml.
    """
    import health_handler

    assert health_handler.VERSION == pyproject_version
