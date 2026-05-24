"""Tests for the `install-preset` / `install-call` CLI.

Mocks at install_preset.py's stable boundaries (`spawn_installer_pod`,
`wait_for_health`, `stream_install`, `shutdown_pod`, `delete_pod`) so we
exercise the orchestration logic without touching RunPod or aiohttp.
"""

from __future__ import annotations

import io
import json

import pytest

from comfy_gen import install_preset


@pytest.fixture
def mocks(monkeypatch):
    state = {
        "spawn_calls": [],
        "spawned_id": "pod-abc",
        "health_raises": None,
        "events": [],
        "shutdowns": [],
        "stream_raises": None,
    }

    def fake_spawn(api_key, image, volume_id, token, name="x", port=3000, **_):
        state["spawn_calls"].append({
            "api_key": api_key, "image": image, "volume_id": volume_id,
            "token": token, "port": port,
        })
        return {"id": state["spawned_id"]}

    def fake_health(pod_id, port, timeout_sec):
        if state["health_raises"]:
            raise RuntimeError(state["health_raises"])

    def fake_stream(pod_id, port, token, preset_id, civitai_token=None, hf_token=None):
        if state["stream_raises"]:
            raise RuntimeError(state["stream_raises"])
        yield from state["events"]

    def fake_shutdown(pod_id, port, token):
        state["shutdowns"].append(pod_id)

    monkeypatch.setattr(install_preset, "spawn_installer_pod", fake_spawn)
    monkeypatch.setattr(install_preset, "wait_for_health", fake_health)
    monkeypatch.setattr(install_preset, "stream_install", fake_stream)
    monkeypatch.setattr(install_preset, "shutdown_pod", fake_shutdown)

    # Config: stub runpod_api_key so spawn path runs.
    monkeypatch.setattr(install_preset.config, "load", lambda: {"runpod_api_key": "rpa_x"})
    return state


def _run_lines(out_buf) -> list[dict]:
    return [json.loads(l) for l in out_buf.getvalue().splitlines() if l.strip()]


def test_happy_path_spawn_install_shutdown_exit_0(mocks):
    mocks["events"] = [
        {"type": "preflight_start"},
        {"type": "preflight_ok", "preset_id": "p", "models_count": 1,
         "total_bytes": 100, "volume_free_bytes": 10**12},
        {"type": "download_start", "file_index": 0, "file": "a"},
        {"type": "download_done", "file_index": 0, "file": "a",
         "cached": False, "bytes": 100, "sha256": "x" * 64},
        {"type": "install_done", "ok": True, "files": [{"filename": "a"}],
         "elapsed_sec": 5},
    ]
    buf = io.StringIO()
    rc = install_preset.run(
        preset_id="qwen", volume_id="vid", pod_id=None, token=None, out=buf,
    )
    assert rc == 0
    lines = _run_lines(buf)
    assert lines[0]["type"] == "pod_spawned"
    assert lines[0]["pod_id"] == "pod-abc"
    assert lines[-1]["type"] == "install_done"
    assert mocks["shutdowns"] == ["pod-abc"]


def test_health_timeout_exits_1_and_does_not_call_stream(mocks):
    mocks["health_raises"] = "not healthy"
    mocks["events"] = [{"type": "install_done", "ok": True}]
    buf = io.StringIO()
    rc = install_preset.run(
        preset_id="qwen", volume_id="vid", pod_id=None, token=None, out=buf,
    )
    assert rc == 1
    lines = _run_lines(buf)
    err = next(l for l in lines if l["type"] == "install_error")
    assert err["stage"] == "health"


def test_install_error_event_propagates_exit_1(mocks):
    mocks["events"] = [
        {"type": "preflight_start"},
        {"type": "install_error", "stage": "download", "reason": "aria2c failed"},
    ]
    buf = io.StringIO()
    rc = install_preset.run(
        preset_id="qwen", volume_id="vid", pod_id=None, token=None, out=buf,
    )
    assert rc == 1
    lines = _run_lines(buf)
    assert any(l["type"] == "install_error" for l in lines)


def test_keep_alive_skips_shutdown(mocks):
    mocks["events"] = [{"type": "install_done", "ok": True, "files": [], "elapsed_sec": 1}]
    buf = io.StringIO()
    rc = install_preset.run(
        preset_id="qwen", volume_id="vid", pod_id=None, token=None,
        keep_alive=True, out=buf,
    )
    assert rc == 0
    assert mocks["shutdowns"] == []


def test_install_call_no_spawn(mocks):
    mocks["events"] = [{"type": "install_done", "ok": True, "files": [], "elapsed_sec": 1}]
    buf = io.StringIO()
    rc = install_preset.run(
        preset_id="qwen", volume_id=None, pod_id="existing-pod",
        token="t-123", out=buf,
    )
    assert rc == 0
    assert mocks["spawn_calls"] == [], "spawn must not be called when pod-id given"
    assert mocks["shutdowns"] == ["existing-pod"]
    lines = _run_lines(buf)
    # No pod_spawned line in install-call mode.
    assert not any(l["type"] == "pod_spawned" for l in lines)


def test_preflight_fail_exits_1(mocks):
    mocks["events"] = [
        {"type": "preflight_start"},
        {"type": "preflight_fail", "reason": "need 1PB"},
    ]
    buf = io.StringIO()
    rc = install_preset.run(
        preset_id="qwen", volume_id="vid", pod_id=None, token=None, out=buf,
    )
    assert rc == 1


def test_install_call_requires_token():
    """A pod-id without a token can't auth — must raise before any HTTP."""
    with pytest.raises(RuntimeError, match="token required"):
        install_preset.run(
            preset_id="qwen", volume_id=None, pod_id="pid",
            token=None, out=io.StringIO(),
        )


def test_spawn_requires_volume_id():
    with pytest.raises(RuntimeError, match="volume-id required"):
        install_preset.run(
            preset_id="qwen", volume_id=None, pod_id=None,
            token=None, out=io.StringIO(),
        )
