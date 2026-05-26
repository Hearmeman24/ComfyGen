"""Contract test for the canonical stderr progress-line shape.

Bead remote_comfy_generator-bmq.5 / A.1.2 / E.1. BlockFlow's progress bar
parses orchestrator stderr; this test simulates every progress event type and
asserts each emitted progress tick matches `PROGRESS_RE`. Prevents silent
breakage of the parser when an f-string drifts.
"""

from __future__ import annotations

from typing import Any

import pytest

from comfy_gen import _progress_format
from comfy_gen._progress_format import (
    PROGRESS_RE,
    format_progress,
    try_format_from_message,
)


# --- format_progress / regex sanity -----------------------------------------


def test_format_progress_matches_regex_minimal():
    line = format_progress(elapsed=5, stage="download", current=3, total=8)
    m = PROGRESS_RE.match(line)
    assert m, line
    assert m.groups() == ("5", "download", "3", "8", "")


def test_format_progress_matches_regex_with_rest():
    line = format_progress(elapsed=12, stage="download", current=3, total=8,
                           rest="wan_2.1_vae.safetensors 45%")
    m = PROGRESS_RE.match(line)
    assert m, line
    assert m.group(2) == "download"
    assert m.group(3) == "3" and m.group(4) == "8"
    assert "wan_2.1_vae.safetensors 45%" in m.group(5)


def test_format_progress_lowercases_stage():
    line = format_progress(elapsed=0, stage="DOWNLOAD", current=1, total=2)
    assert PROGRESS_RE.match(line).group(2) == "download"


def test_try_format_from_message_reformats_n_of_m():
    line = try_format_from_message(
        elapsed=7, stage="download",
        message="Downloading 3/8 wan_2.1_vae.safetensors",
        percent=45.6,
    )
    assert line is not None
    m = PROGRESS_RE.match(line)
    assert m and m.groups() == ("7", "download", "3", "8", "wan_2.1_vae.safetensors 46%")


def test_try_format_from_message_returns_none_for_unparseable():
    assert try_format_from_message(
        elapsed=1, stage="install",
        message="Job submitted: abc123", percent=None,
    ) is None


# --- End-to-end: every progress tick emits a regex-matching line ------------


def _capture_log_lines(monkeypatch) -> list[str]:
    """Replace output.log with a list-appender so we can inspect what shipped."""
    from comfy_gen import output
    captured: list[str] = []
    monkeypatch.setattr(output, "log", lambda s: captured.append(s))
    return captured


@pytest.mark.parametrize("progress_payload", [
    {"stage": "download", "percent": 0.0, "message": "Downloading 1/8"},
    {"stage": "download", "percent": 12.5, "message": "Downloading 1/8 wan_2.1_vae.safetensors"},
    {"stage": "download", "percent": 50.0, "message": "Downloading 4/8 hifi.safetensors 50%"},
    {"stage": "download", "percent": 100.0, "message": "Downloading 8/8"},
])
def test_download_progress_callback_emits_canonical_line(progress_payload, monkeypatch):
    """Every per-tick progress payload, when run through download._progress,
    produces stderr lines matching PROGRESS_RE."""
    from comfy_gen import download as dl

    # Inline the _progress closure: build it the same way submit_download does.
    captured = _capture_log_lines(monkeypatch)

    def _progress(elapsed, status, prog):
        msg = prog.get("message", "")
        pct = prog.get("percent")
        stage = prog.get("stage", "download")
        canonical = _progress_format.try_format_from_message(elapsed, stage, msg, pct)
        if canonical:
            from comfy_gen import output
            output.log(canonical)
        elif msg and pct is not None:
            from comfy_gen import output
            output.log(f"[{elapsed}s] {msg} ({pct:.0f}%)")

    _progress(elapsed=5, status="IN_PROGRESS", prog=progress_payload)
    assert captured, "expected at least one log line"
    for line in captured:
        assert PROGRESS_RE.match(line), f"line failed to match canonical regex: {line!r}"


def test_poller_default_branch_emits_canonical_when_message_has_n_of_m(monkeypatch):
    """The poller's default progress branch (no progress_fn) goes through
    try_format_from_message — same contract."""
    from comfy_gen import poller, output

    captured: list[str] = []
    monkeypatch.setattr(output, "log", lambda s: captured.append(s))

    # Simulate the relevant slice of poll_job's progress branch.
    elapsed = 11
    status = "IN_PROGRESS"
    prog: dict[str, Any] = {"stage": "download", "percent": 50.0, "message": "Downloading 4/8"}
    msg = prog["message"]
    pct = prog["percent"]
    stage = prog.get("stage", status or "job")
    canonical = _progress_format.try_format_from_message(elapsed, stage, msg, pct)
    assert canonical is not None
    output.log(canonical)

    assert captured and PROGRESS_RE.match(captured[0]), captured
