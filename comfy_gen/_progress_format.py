"""Canonical stderr progress line format for `comfy-gen` long-running jobs.

Bead remote_comfy_generator-bmq.5 / A.1.2 / E.1. BlockFlow tails the
orchestrator's stderr to drive a progress bar in the UI; without a stable line
shape, a stray f-string tweak silently breaks the bar.

## The canonical format

Every progress *tick* line (one per polling iteration of a download / install)
matches:

    [<elapsed>s] <stage>: (<current>/<total>) <rest>

Where:
- `<elapsed>`  — integer seconds since the orchestrator submitted the job
- `<stage>`    — lowercase verb (`download`, `install`, `preflight`, ...); the
                 worker's `progress_update` payload's `stage` field
- `<current>`  — integer, current item index 1-based (e.g. file 3 of 8)
- `<total>`    — integer, total item count
- `<rest>`     — freeform text (filename, speed, percent, etc.) — may be empty

Status / one-shot lines ("Job submitted: ...", "Download complete: ...") are
*not* progress ticks and do NOT need to match this regex; the contract test
only iterates lines emitted by the per-tick path.

The regex `PROGRESS_RE` is the single source of truth. BlockFlow's progress
parser MUST reference the same regex shape (documented; no runtime import to
keep the repos decoupled).
"""

from __future__ import annotations

import re

PROGRESS_RE = re.compile(r"\[(\d+)s\]\s+(\w+):\s+\((\d+)/(\d+)\)\s*(.*)")

_MSG_NM_RE = re.compile(r"(?P<verb>\w+)\s+(?P<current>\d+)/(?P<total>\d+)\s*(?P<rest>.*)")


def format_progress(elapsed: int, stage: str, current: int, total: int, rest: str = "") -> str:
    """Build a progress tick line guaranteed to match `PROGRESS_RE`.

    `stage` is lowercased so the regex captures a normalized verb.
    `rest` is trimmed; an empty rest still emits a trailing space-free shape.
    """
    rest = rest.strip()
    if rest:
        return f"[{int(elapsed)}s] {stage.lower()}: ({int(current)}/{int(total)}) {rest}"
    return f"[{int(elapsed)}s] {stage.lower()}: ({int(current)}/{int(total)})"


def try_format_from_message(elapsed: int, stage: str, message: str, percent: float | None = None) -> str | None:
    """Best-effort reformat of an arbitrary progress `message` into canonical form.

    The worker emits messages like "Downloading 3/8" via runpod's
    `progress_update`. When a message contains `<verb> N/M`, return the
    canonical line; otherwise return None and let the caller fall back.
    """
    m = _MSG_NM_RE.search(message)
    if not m:
        return None
    rest_parts: list[str] = []
    msg_rest = m.group("rest").strip()
    if msg_rest:
        rest_parts.append(msg_rest)
    if percent is not None:
        rest_parts.append(f"{percent:.0f}%")
    return format_progress(
        elapsed=elapsed,
        stage=stage or m.group("verb").lower(),
        current=int(m.group("current")),
        total=int(m.group("total")),
        rest=" ".join(rest_parts),
    )
