"""Tests for smoke_preset's schema helpers.

Covers the multi-workflow + smoke_inputs handling added in
remote_comfy_generator-kv9. The full smoke loop is integration-tested
live against a real RunPod endpoint (see automation/smoke_preset.py);
these are unit tests for the pure helpers only.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add automation/ to path so we can import smoke_preset's pure helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "automation"))

from smoke_preset import (  # noqa: E402
    SupplyConstrainedError,
    choose_workflow,
    fetch_smoke_inputs,
    resolve_volume_for_endpoint,
    run_install_preset,
    run_with_retry,
)


def test_choose_workflow_picks_legacy_singular():
    preset = {"workflow": {"url": "https://x/wf.json", "sha256": "abc"}}
    assert choose_workflow(preset) == {"url": "https://x/wf.json", "sha256": "abc"}


def test_choose_workflow_picks_first_from_array():
    preset = {
        "workflows": [
            {"name": "Keep Background", "url": "https://x/kb.json", "sha256": "1"},
            {"name": "Replace Character", "url": "https://x/rc.json", "sha256": "2"},
        ]
    }
    chosen = choose_workflow(preset)
    assert chosen["name"] == "Keep Background"


def test_choose_workflow_empty_workflows_raises():
    with pytest.raises(RuntimeError, match="neither 'workflow' nor"):
        choose_workflow({"workflows": []})


def test_choose_workflow_missing_both_raises():
    with pytest.raises(RuntimeError):
        choose_workflow({"id": "x", "name": "y"})


def test_fetch_smoke_inputs_downloads_and_verifies(tmp_path, monkeypatch):
    """Each fixture is downloaded, sha256-verified, written, and returned."""
    payload = b"hello fixture bytes"
    sha = hashlib.sha256(payload).hexdigest()
    smoke_inputs = [
        {
            "node_id": "311",
            "field": "image",
            "url": "https://example.com/img.png",
            "sha256": sha,
            "filename": "img.png",
        }
    ]

    class FakeResp:
        def read(self_inner): return payload

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        result = fetch_smoke_inputs(smoke_inputs, preset_id="testp")

    assert len(result) == 1
    node_id, local_path = result[0]
    assert node_id == "311"
    assert local_path.endswith("img.png")
    assert Path(local_path).read_bytes() == payload


def test_fetch_smoke_inputs_sha_mismatch_raises():
    smoke_inputs = [
        {
            "node_id": "311",
            "field": "image",
            "url": "https://example.com/img.png",
            "sha256": "deadbeef" * 8,  # 64 chars but wrong
            "filename": "img.png",
        }
    ]

    class FakeResp:
        def read(self_inner): return b"some bytes"

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        with pytest.raises(RuntimeError, match="sha256 mismatch"):
            fetch_smoke_inputs(smoke_inputs, preset_id="testp")


def test_fetch_smoke_inputs_multiple_fixtures_all_returned():
    inputs = [
        {"node_id": "311", "field": "image", "url": "u1",
         "sha256": hashlib.sha256(b"a").hexdigest(), "filename": "a.png"},
        {"node_id": "417", "field": "video", "url": "u2",
         "sha256": hashlib.sha256(b"bb").hexdigest(), "filename": "b.mp4"},
    ]

    class FakeResp:
        def __init__(self, data): self.data = data
        def read(self): return self.data

    fake_responses = [FakeResp(b"a"), FakeResp(b"bb")]
    with patch("urllib.request.urlopen", side_effect=fake_responses):
        result = fetch_smoke_inputs(inputs, preset_id="testp")

    assert len(result) == 2
    assert [n for n, _ in result] == ["311", "417"]


# --- run_install_preset: line-delimited JSON event stream consumption ---
#
# The new `comfy-gen install-preset` emits one JSON event per stdout line
# (not one final JSON blob). The helper drives the subprocess via Popen,
# yields events to a sink, and returns the {files, cached, fresh} summary
# from the terminal install_done event.

import json as _json
import subprocess as _subprocess


def _fake_popen_with_events(events: list[dict], returncode: int = 0):
    """Return a context-manager-friendly fake Popen yielding the given events."""

    class FakePopen:
        def __init__(self, *_a, **_kw):
            self.stdout = iter(_json.dumps(e).encode() + b"\n" for e in events)
            self.returncode = returncode
            self.stderr = b""
            self.args = _a
            self.kwargs = _kw

        def wait(self, timeout=None):
            return self.returncode

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return FakePopen


def test_run_install_preset_happy_path_returns_summary():
    events = [
        {"type": "pod_spawned", "pod_id": "p", "token": "t"},
        {"type": "preflight_start"},
        {"type": "preflight_ok", "preset_id": "qwen-image-lighting",
         "models_count": 4, "total_bytes": 0, "volume_free_bytes": 10**12},
        {"type": "download_start", "file_index": 0, "file": "a"},
        {"type": "download_done",  "file_index": 0, "file": "a",
         "cached": True,  "bytes": 1, "sha256": "x" * 64},
        {"type": "download_start", "file_index": 1, "file": "b"},
        {"type": "download_done",  "file_index": 1, "file": "b",
         "cached": False, "bytes": 2, "sha256": "y" * 64},
        {"type": "install_done", "ok": True,
         "files": [{"filename": "a", "cached": True},
                   {"filename": "b", "cached": False}],
         "elapsed_sec": 37},
    ]
    with patch.object(_subprocess, "Popen", _fake_popen_with_events(events)):
        out = run_install_preset(preset_id="qwen-image-lighting", volume_id="v")
    assert out["total"] == 2
    assert out["cached"] == 1
    assert out["fresh"] == 1
    assert out["elapsed_sec"] == 37
    assert len(out["files"]) == 2


def test_run_install_preset_install_error_raises():
    events = [
        {"type": "preflight_start"},
        {"type": "preflight_ok", "preset_id": "x", "models_count": 1,
         "total_bytes": 0, "volume_free_bytes": 10**12},
        {"type": "download_start", "file_index": 0, "file": "a"},
        {"type": "install_error", "stage": "download", "reason": "aria2c blew up"},
    ]
    with patch.object(_subprocess, "Popen", _fake_popen_with_events(events, returncode=1)):
        with pytest.raises(RuntimeError, match="aria2c blew up"):
            run_install_preset(preset_id="x", volume_id="v")


def test_run_install_preset_preflight_fail_raises():
    events = [
        {"type": "preflight_start"},
        {"type": "preflight_fail", "reason": "preset_id 'nope' missing"},
    ]
    with patch.object(_subprocess, "Popen", _fake_popen_with_events(events, returncode=1)):
        with pytest.raises(RuntimeError, match="missing"):
            run_install_preset(preset_id="nope", volume_id="v")


def test_run_install_preset_no_terminal_event_raises():
    events = [
        {"type": "preflight_start"},
        {"type": "preflight_ok", "preset_id": "x", "models_count": 0,
         "total_bytes": 0, "volume_free_bytes": 0},
    ]
    # Exit non-zero with no install_done / install_error / preflight_fail.
    with patch.object(_subprocess, "Popen", _fake_popen_with_events(events, returncode=2)):
        with pytest.raises(RuntimeError, match="unexpected exit"):
            run_install_preset(preset_id="x", volume_id="v")


def test_run_install_preset_install_done_ok_false_raises():
    events = [
        {"type": "preflight_ok", "preset_id": "x", "models_count": 1,
         "total_bytes": 0, "volume_free_bytes": 10**12},
        {"type": "install_done", "ok": False, "files": [], "elapsed_sec": 1},
    ]
    with patch.object(_subprocess, "Popen", _fake_popen_with_events(events, returncode=1)):
        with pytest.raises(RuntimeError, match="install_done.ok=False"):
            run_install_preset(preset_id="x", volume_id="v")


# --- resolve_volume_for_endpoint: GET /v1/endpoints/<ep> ---

def test_resolve_volume_for_endpoint_returns_first_volume():
    class FakeResp:
        def __init__(self, payload): self.payload = _json.dumps(payload).encode()
        def read(self): return self.payload
        def __enter__(self): return self
        def __exit__(self, *_): return False

    with patch("urllib.request.urlopen",
               return_value=FakeResp({"networkVolumeIds": ["vol-abc"]})):
        assert resolve_volume_for_endpoint("rpa_x", "ep-1") == "vol-abc"


def test_resolve_volume_for_endpoint_empty_list_raises():
    class FakeResp:
        def __init__(self, payload): self.payload = _json.dumps(payload).encode()
        def read(self): return self.payload
        def __enter__(self): return self
        def __exit__(self, *_): return False

    with patch("urllib.request.urlopen",
               return_value=FakeResp({"networkVolumeIds": []})):
        with pytest.raises(RuntimeError, match="no network volume"):
            resolve_volume_for_endpoint("rpa_x", "ep-1")


def test_resolve_volume_for_endpoint_singular_field_fallback():
    """Some endpoint responses report `networkVolumeId` (singular) instead."""
    class FakeResp:
        def __init__(self, payload): self.payload = _json.dumps(payload).encode()
        def read(self): return self.payload
        def __enter__(self): return self
        def __exit__(self, *_): return False

    with patch("urllib.request.urlopen",
               return_value=FakeResp({"networkVolumeId": "vol-singular"})):
        assert resolve_volume_for_endpoint("rpa_x", "ep-1") == "vol-singular"


# --- supply-constraint detection + retry policy ---


def test_run_install_preset_surfaces_status_error_stdout():
    """When the CLI emits {status:error,error:...} on stdout (e.g. RuntimeError
    caught by main()), run_install_preset must include the message in its
    RuntimeError instead of swallowing it as `stderr tail: (empty)`."""
    events = [
        {"status": "error",
         "error": "pod spawn failed — no CPU instance available for any of "
                  "['cpu3c-2-4']; last=SUPPLY_CONSTRAINT"},
    ]
    with patch.object(_subprocess, "Popen", _fake_popen_with_events(events, returncode=1)):
        with pytest.raises(SupplyConstrainedError, match="SUPPLY_CONSTRAINT"):
            run_install_preset(preset_id="x", volume_id="v")


def test_run_install_preset_non_supply_error_still_runtimeerror():
    """A non-capacity status:error must still raise (just plain RuntimeError,
    not the SupplyConstrainedError subclass) so the caller doesn't retry."""
    events = [
        {"status": "error", "error": "runpod_api_key not configured"},
    ]
    with patch.object(_subprocess, "Popen", _fake_popen_with_events(events, returncode=1)):
        with pytest.raises(RuntimeError, match="runpod_api_key not configured") as ei:
            run_install_preset(preset_id="x", volume_id="v")
        assert not isinstance(ei.value, SupplyConstrainedError)


def test_run_with_retry_succeeds_after_supply_constraint():
    """Caller retries on SupplyConstrainedError until smoke() succeeds."""
    calls = {"n": 0}

    def attempt():
        calls["n"] += 1
        if calls["n"] < 3:
            raise SupplyConstrainedError("SUPPLY_CONSTRAINT (attempt < 3)")
        return {"ok": True, "preset_id": "x"}

    # Patch sleep so the test is fast; backoff schedule is exercised via calls.
    sleeps: list[float] = []
    with patch("smoke_preset.time.sleep", side_effect=sleeps.append):
        result = run_with_retry(attempt, max_wall_seconds=300)
    assert result == {"ok": True, "preset_id": "x"}
    assert calls["n"] == 3
    # Backoff is monotonically increasing (or capped); never zero.
    assert all(s > 0 for s in sleeps)
    assert sleeps == sorted(sleeps)


def test_run_with_retry_skips_after_wall_clock_budget():
    """After max_wall_seconds of capacity errors, return a skip envelope (don't raise)."""
    def attempt():
        raise SupplyConstrainedError("SUPPLY_CONSTRAINT")

    # Fake monotonic clock: each call to time.monotonic advances 60 seconds.
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 60.0
        return clock["t"]

    with patch("smoke_preset.time.sleep"), \
         patch("smoke_preset.time.monotonic", side_effect=fake_monotonic):
        result = run_with_retry(attempt, max_wall_seconds=300)
    assert result["ok"] is True
    assert result["skipped"] is True
    assert "supply" in result["reason"].lower() or "capacity" in result["reason"].lower()


def test_run_with_retry_non_supply_error_propagates():
    """A non-supply-constraint exception must NOT be retried; it should bubble."""
    def attempt():
        raise RuntimeError("install_done.ok=False")

    with patch("smoke_preset.time.sleep"):
        with pytest.raises(RuntimeError, match="install_done.ok=False"):
            run_with_retry(attempt, max_wall_seconds=300)
