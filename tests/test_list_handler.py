"""Tests for the `list_models` worker command, focused on the extension allowlist.

list_handler filters scanned files to those with a model-shaped extension. The
filter must include `.onnx` and `.gguf` so custom-node-managed models (ONNX
detection, GGUF quantized) show up alongside .safetensors/.ckpt/.bin/.pt/.pth.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def fake_models(tmp_path, monkeypatch):
    """Point list_handler at a tmp models tree; return the (comfyui, volume) root paths."""
    import list_handler
    comfy = tmp_path / "comfyui-models"
    volume = tmp_path / "volume-models"
    comfy.mkdir()
    volume.mkdir()
    monkeypatch.setattr(list_handler, "COMFYUI_MODELS", str(comfy))
    monkeypatch.setattr(list_handler, "VOLUME_MODELS", str(volume))
    # Empty extra_model_paths.yaml path so the handler doesn't try the real file.
    monkeypatch.setattr(list_handler, "EXTRA_PATHS_FILE", str(tmp_path / "missing.yaml"))
    return comfy, volume


def _touch(path, size: int = 16) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return str(path)


def test_list_includes_onnx_files(fake_models, dispatch_command):
    _, volume = fake_models
    _touch(volume / "detection" / "yolov10m.onnx", size=1024)
    _touch(volume / "detection" / "vitpose.onnx", size=2048)
    _touch(volume / "detection" / "vitpose_data.bin", size=4096)

    res = dispatch_command({"command": "list_models", "model_type": "detection"})
    filenames = sorted(f["filename"] for f in res["files"])
    assert filenames == ["vitpose.onnx", "vitpose_data.bin", "yolov10m.onnx"]


def test_list_includes_gguf_files(fake_models, dispatch_command):
    _, volume = fake_models
    _touch(volume / "unet" / "flux.gguf", size=1024)
    _touch(volume / "unet" / "sdxl.safetensors", size=2048)

    res = dispatch_command({"command": "list_models", "model_type": "unet"})
    filenames = sorted(f["filename"] for f in res["files"])
    assert filenames == ["flux.gguf", "sdxl.safetensors"]


def test_list_still_filters_random_extensions(fake_models, dispatch_command):
    _, volume = fake_models
    _touch(volume / "loras" / "real.safetensors")
    _touch(volume / "loras" / "readme.txt")
    _touch(volume / "loras" / "preview.png")

    res = dispatch_command({"command": "list_models", "model_type": "loras"})
    filenames = sorted(f["filename"] for f in res["files"])
    assert filenames == ["real.safetensors"]


def test_list_existing_allowlist_still_works(fake_models, dispatch_command):
    """Regression guard for the original extensions."""
    _, volume = fake_models
    for ext in (".safetensors", ".ckpt", ".pt", ".pth", ".bin"):
        _touch(volume / "checkpoints" / f"m{ext}")

    res = dispatch_command({"command": "list_models", "model_type": "checkpoints"})
    filenames = sorted(f["filename"] for f in res["files"])
    assert filenames == ["m.bin", "m.ckpt", "m.pt", "m.pth", "m.safetensors"]
