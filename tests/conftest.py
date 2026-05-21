"""Shared fixtures for serverless-runtime handler tests.

`serverless-runtime/` is deployed as a separate Docker image and is not a Python
package; we expose its modules to tests by inserting it on `sys.path` here (also
configured in pyproject.toml `[tool.pytest.ini_options].pythonpath`, but doing it
in-process keeps `python -m pytest` working from anywhere).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVERLESS_RUNTIME = REPO_ROOT / "serverless-runtime"

if str(SERVERLESS_RUNTIME) not in sys.path:
    sys.path.insert(0, str(SERVERLESS_RUNTIME))


@pytest.fixture
def dispatch_command():
    """Dispatch a worker command through the same path the runtime uses.

    Returns a callable that takes a job input dict (e.g. `{"command": "health"}`)
    and returns the handler's result. Mirrors the dispatch block in worker.py so
    tests exercise the real routing logic without booting RunPod.
    """
    def _dispatch(job_input: dict) -> dict:
        command = job_input.get("command")
        job = {"id": "test-job", "input": job_input}
        if command == "health":
            import health_handler
            return health_handler.handle(job)
        if command == "delete":
            import delete_handler
            return delete_handler.handle(job)
        if command == "volume_info":
            import volume_info_handler
            return volume_info_handler.handle(job)
        if command == "hash":
            import hash_handler
            return hash_handler.handle(job)
        raise ValueError(f"unknown command: {command!r}")

    return _dispatch


@pytest.fixture
def pyproject_version() -> str:
    """The version string declared in pyproject.toml, read at test time.

    Source of truth for what the worker's /health command should report.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        line = line.strip()
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("version not found in pyproject.toml")
