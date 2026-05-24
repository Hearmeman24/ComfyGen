"""Tests for preset_resolver — fetches the BlockFlow preset manifest and
returns a parsed preset dict + a download_handler-compatible batch.

The HTTP layer is mocked at urllib.request.urlopen; the parsing/translation
logic is exercised against real bytes.
"""

from __future__ import annotations

import io
import json

import pytest

import preset_resolver


MANIFEST = {
    "presets": [
        {"id": "qwen-image-lighting", "preset_url": "https://example.com/qwen.json"},
        {"id": "wan-video", "preset_url": "https://example.com/wan.json"},
    ]
}

PRESET = {
    "id": "qwen-image-lighting",
    "models": [
        {"url": "https://huggingface.co/a.safetensors",
         "dest": "loras/qwen/a.safetensors",
         "sha256": "a" * 64},
        {"url": "https://huggingface.co/b.safetensors",
         "dest": "checkpoints/b.safetensors",
         "sha256": "b" * 64},
    ],
}


@pytest.fixture
def fake_urlopen(monkeypatch):
    """Map URL → bytes; raise KeyError on miss so missing URLs are loud."""
    state = {"calls": []}

    def factory(routes: dict[str, bytes]):
        def _open(url, timeout=None):
            state["calls"].append(url)
            if url not in routes:
                raise KeyError(f"unexpected url: {url}")
            return io.BytesIO(routes[url])
        monkeypatch.setattr(preset_resolver.urllib.request, "urlopen", _open)
        return state

    return factory


def test_resolve_preset_happy_path(fake_urlopen):
    fake_urlopen({
        "https://example.com/manifest.json": json.dumps(MANIFEST).encode(),
        "https://example.com/qwen.json": json.dumps(PRESET).encode(),
    })
    out = preset_resolver.resolve_preset(
        "qwen-image-lighting",
        manifest_url="https://example.com/manifest.json",
    )
    assert out == PRESET


def test_resolve_preset_unknown_id_raises(fake_urlopen):
    fake_urlopen({"https://example.com/manifest.json": json.dumps(MANIFEST).encode()})
    with pytest.raises(KeyError, match="missing"):
        preset_resolver.resolve_preset(
            "does-not-exist",
            manifest_url="https://example.com/manifest.json",
        )


def test_resolve_preset_malformed_manifest_raises(fake_urlopen):
    fake_urlopen({"https://example.com/manifest.json": b"not json{{{"})
    with pytest.raises(json.JSONDecodeError):
        preset_resolver.resolve_preset(
            "any",
            manifest_url="https://example.com/manifest.json",
        )


def test_preset_to_download_batch_translates_models():
    batch = preset_resolver.preset_to_download_batch(PRESET)
    assert batch == [
        {"source": "url", "url": "https://huggingface.co/a.safetensors",
         "destination_path": "loras/qwen/a.safetensors", "sha256": "a" * 64},
        {"source": "url", "url": "https://huggingface.co/b.safetensors",
         "destination_path": "checkpoints/b.safetensors", "sha256": "b" * 64},
    ]


def test_preset_to_download_batch_handles_empty_models():
    """Workflow-only preset — no models to download is a valid shape."""
    assert preset_resolver.preset_to_download_batch({"id": "x"}) == []
    assert preset_resolver.preset_to_download_batch({"id": "x", "models": []}) == []
