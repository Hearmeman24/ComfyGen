"""Consistent JSON output formatting for the CLI."""

import json
import sys
from typing import Any


def success(data: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Print a success JSON response to stdout and exit 0."""
    result = {"status": "ok"}
    if data:
        result.update(data)
    result.update(kwargs)
    print(json.dumps(result))
    sys.exit(0)


def error(message: str, code: int = 1, **kwargs: Any) -> None:
    """Print an error JSON response to stdout and exit with given code."""
    result = {"status": "error", "error": message}
    result.update(kwargs)
    print(json.dumps(result))
    sys.exit(code)


def log(message: str) -> None:
    """Print a human-readable log message to stderr (never pollutes JSON stdout)."""
    print(message, file=sys.stderr)
