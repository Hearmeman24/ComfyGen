"""Tests for installer_server (bead 5f2).

We drive the aiohttp app via its test client. Real HTTP, mocked download
handler + preset_resolver + volume_info_handler so we can assert event
shapes without touching the network or the filesystem.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import download_handler
import installer_server
import preset_resolver
import volume_info_handler


TOKEN = "secret-token-abc123"
PRESET_ID = "qwen-image-lighting"


PRESET = {
    "id": PRESET_ID,
    "models": [
        {"url": "https://example.com/a.safetensors",
         "dest": "loras/a.safetensors",
         "sha256": "a" * 64,
         "bytes": 1024},
    ],
}


@pytest.fixture
async def client(aiohttp_client, monkeypatch):
    # Volume info returns generous free space.
    monkeypatch.setattr(volume_info_handler, "handle", lambda job: {
        "ok": True, "path": "/runpod-volume",
        "size_bytes": 10**12, "used_bytes": 0, "free_bytes": 10**12,
    })
    app = installer_server.build_app(token=TOKEN, idle_timeout_sec=3600)
    return await aiohttp_client(app)


async def _consume_sse(resp) -> list[dict]:
    events: list[dict] = []
    async for raw in resp.content:
        line = raw.decode().rstrip("\n")
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def test_health_no_auth_required(client):
    r = await client.get("/health")
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["ready"] is True
    assert "version" in body


async def test_volume_info_requires_token(client):
    r = await client.get("/volume_info")
    assert r.status == 401
    body = await r.json()
    assert "missing" in body["reason"].lower()


async def test_volume_info_with_token_delegates(client):
    r = await client.get("/volume_info", headers={"X-Installer-Token": TOKEN})
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["free_bytes"] == 10**12


async def test_volume_info_with_wrong_token_401(client):
    r = await client.get("/volume_info", headers={"X-Installer-Token": "wrong"})
    assert r.status == 401


async def test_install_streams_full_event_sequence(client, monkeypatch):
    monkeypatch.setattr(preset_resolver, "resolve_preset", lambda pid, **kw: PRESET)

    def fake_download(job, progress_callback=None):
        if progress_callback:
            progress_callback({"type": "download_start", "file_index": 0, "file": "a.safetensors"})
            progress_callback({"type": "download_done", "file_index": 0,
                               "file": "a.safetensors", "cached": False,
                               "bytes": 1024, "sha256": "a" * 64})
        return {"ok": True, "files": [{"filename": "a.safetensors"}]}

    monkeypatch.setattr(download_handler, "handle", fake_download)

    r = await client.post(f"/install/{PRESET_ID}", headers={"X-Installer-Token": TOKEN})
    assert r.status == 200
    events = await _consume_sse(r)
    types = [e["type"] for e in events]
    assert types[0] == "preflight_start"
    assert "preflight_ok" in types
    assert "download_start" in types
    assert "download_done" in types
    assert types[-1] == "install_done"
    done = events[-1]
    assert done["ok"] is True
    assert "elapsed_sec" in done


async def test_install_unknown_preset_emits_preflight_fail(client, monkeypatch):
    def boom(pid, **kw):
        raise KeyError(f"preset_id {pid!r} missing from manifest")
    monkeypatch.setattr(preset_resolver, "resolve_preset", boom)

    r = await client.post("/install/does-not-exist",
                          headers={"X-Installer-Token": TOKEN})
    events = await _consume_sse(r)
    types = [e["type"] for e in events]
    assert "preflight_fail" in types
    assert "install_done" not in types


async def test_install_insufficient_space_emits_preflight_fail(client, monkeypatch):
    big_preset = {
        "id": PRESET_ID,
        "models": [{"url": "u", "dest": "d/f", "sha256": "x" * 64,
                    "bytes": 10**15}],  # 1 PB, exceeds free
    }
    monkeypatch.setattr(preset_resolver, "resolve_preset", lambda pid, **kw: big_preset)
    fake_dl = lambda *a, **kw: pytest.fail("download_handler should not be called")
    monkeypatch.setattr(download_handler, "handle", fake_dl)

    r = await client.post(f"/install/{PRESET_ID}", headers={"X-Installer-Token": TOKEN})
    events = await _consume_sse(r)
    fail = next(e for e in events if e["type"] == "preflight_fail")
    assert fail["need_bytes"] == 10**15


async def test_install_handler_exception_emits_install_error(client, monkeypatch):
    monkeypatch.setattr(preset_resolver, "resolve_preset", lambda pid, **kw: PRESET)

    def boom(job, progress_callback=None):
        raise RuntimeError("aria2c blew up")
    monkeypatch.setattr(download_handler, "handle", boom)

    r = await client.post(f"/install/{PRESET_ID}", headers={"X-Installer-Token": TOKEN})
    events = await _consume_sse(r)
    err = next(e for e in events if e["type"] == "install_error")
    assert err["stage"] == "download"
    assert "aria2c blew up" in err["reason"]


async def test_install_concurrent_call_returns_409(client, monkeypatch):
    monkeypatch.setattr(preset_resolver, "resolve_preset", lambda pid, **kw: PRESET)

    held = asyncio.Event()
    release = asyncio.Event()

    def slow_download(job, progress_callback=None):
        # signal we're in, then block
        import asyncio as _aio
        _aio.run_coroutine_threadsafe(_set(held), _aio.get_event_loop()) if False else None
        held.set()
        # busy-wait on the event (sync function in executor)
        while not release.is_set():
            import time as _t
            _t.sleep(0.01)
        return {"ok": True, "files": []}

    async def _set(ev): ev.set()

    monkeypatch.setattr(download_handler, "handle", slow_download)

    first = asyncio.create_task(
        client.post(f"/install/{PRESET_ID}", headers={"X-Installer-Token": TOKEN})
    )
    # Wait until the first install is actually mid-flight
    await asyncio.wait_for(held.wait(), timeout=5)

    r2 = await client.post(f"/install/{PRESET_ID}", headers={"X-Installer-Token": TOKEN})
    assert r2.status == 409
    body = await r2.json()
    assert "in progress" in body["reason"]

    release.set()
    r1 = await first
    await _consume_sse(r1)  # drain first stream so the test exits cleanly


async def test_shutdown_returns_ok_and_schedules_terminate(client, monkeypatch):
    calls = []

    async def fake_terminate(pod_id, api_key):
        calls.append((pod_id, api_key))

    monkeypatch.setattr(installer_server, "_self_terminate", fake_terminate)
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-xyz")
    monkeypatch.setenv("RUNPOD_API_KEY", "k")
    r = await client.post("/shutdown", headers={"X-Installer-Token": TOKEN})
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["terminating"] is True
    # give the create_task a tick
    await asyncio.sleep(0.05)
    assert calls and calls[0] == ("pod-xyz", "k")
