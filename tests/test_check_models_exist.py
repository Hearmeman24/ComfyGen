"""Tests for the missing-models preflight + auto-downloaded-models allowlist.

Bead remote_comfy_generator-xud. Custom nodes like ComfyUI-Frame-Interpolation
fetch their models (rife49.pth, etc.) on first use, so a missing file on the
volume is not a blocking precondition. The allowlist excludes them from the
returned `missing` list and surfaces them on a separate `auto_downloaded` list
for UI visibility.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def worker(monkeypatch):
    """Import worker and stub the parts of it the preflight depends on.

    `worker.py` calls runpod.serverless.start() at module load, which sys.exits
    when there's no test_input.json. We patch runpod.serverless.start to a
    no-op BEFORE importing.
    """
    import runpod.serverless
    monkeypatch.setattr(runpod.serverless, "start", lambda *a, **k: None)

    # If a prior test imported worker, drop it so module-level constants
    # (notably _AUTO_DOWNLOADED_MODELS_DEFAULT) re-read env vars cleanly.
    import sys
    sys.modules.pop("worker", None)
    import worker

    # Pretend ComfyUI-Manager has no auto-download info — keeps tests offline.
    monkeypatch.setattr(worker, "_get_manager_model_list", lambda: {})
    return worker


def _wf_with_models(*filenames: str) -> dict:
    """Build a minimal API-format workflow that references the given model
    filenames via a `ckpt_name` field on a CheckpointLoader node each."""
    return {
        str(i): {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": fn},
        }
        for i, fn in enumerate(filenames, start=1)
    }


def test_rife49_allowlisted_by_default(worker, monkeypatch):
    monkeypatch.setattr(worker, "_resolve_model_path", lambda fn: None)
    missing, auto = worker._check_models_exist(_wf_with_models("rife49.pth"))
    assert missing == []
    assert [m["filename"] for m in auto] == ["rife49.pth"]


def test_all_rife_variants_allowlisted(worker, monkeypatch):
    monkeypatch.setattr(worker, "_resolve_model_path", lambda fn: None)
    rifes = [f"rife{n}.pth" for n in (40, 41, 42, 43, 44, 45, 46, 47, 48, 49)]
    missing, auto = worker._check_models_exist(_wf_with_models(*rifes))
    assert missing == []
    assert sorted(m["filename"] for m in auto) == sorted(rifes)


def test_genuinely_missing_model_still_blocks(worker, monkeypatch):
    monkeypatch.setattr(worker, "_resolve_model_path", lambda fn: None)
    missing, auto = worker._check_models_exist(_wf_with_models("not_a_real_model.safetensors"))
    assert [m["filename"] for m in missing] == ["not_a_real_model.safetensors"]
    assert auto == []


def test_mixed_workflow_splits_correctly(worker, monkeypatch):
    # Resolved file ⇒ excluded from both lists.
    monkeypatch.setattr(worker, "_resolve_model_path",
                        lambda fn: "/fake/wan.safetensors" if fn == "wan.safetensors" else None)
    missing, auto = worker._check_models_exist(
        _wf_with_models("wan.safetensors", "rife49.pth", "mystery.ckpt")
    )
    assert {m["filename"] for m in missing} == {"mystery.ckpt"}
    assert {m["filename"] for m in auto} == {"rife49.pth"}


def test_env_var_extends_allowlist(worker, monkeypatch):
    monkeypatch.setenv("COMFY_GEN_IGNORE_MISSING_MODELS",
                       "GFPGANv1.4.pth, codeformer.pth")
    monkeypatch.setattr(worker, "_resolve_model_path", lambda fn: None)
    missing, auto = worker._check_models_exist(
        _wf_with_models("GFPGANv1.4.pth", "codeformer.pth", "still_missing.ckpt")
    )
    assert {m["filename"] for m in missing} == {"still_missing.ckpt"}
    assert {m["filename"] for m in auto} == {"GFPGANv1.4.pth", "codeformer.pth"}


def test_env_var_handles_whitespace_and_empties(worker, monkeypatch):
    monkeypatch.setenv("COMFY_GEN_IGNORE_MISSING_MODELS", "  , extra.pth ,,,")
    allowlist = worker._auto_downloaded_models()
    assert "extra.pth" in allowlist
    # No empty strings or whitespace-only entries leaked in.
    assert "" not in allowlist
    assert all(s.strip() == s for s in allowlist)


def test_empty_env_var_keeps_defaults(worker, monkeypatch):
    monkeypatch.delenv("COMFY_GEN_IGNORE_MISSING_MODELS", raising=False)
    allowlist = worker._auto_downloaded_models()
    assert "rife49.pth" in allowlist


def test_resolve_model_path_finds_subfolder_qualified_lora(worker, monkeypatch, tmp_path):
    """After the loras reorg, workflows reference LoRAs by their loras-root-
    relative path (e.g. 'wan2.2/_accel/x.safetensors'). os.walk yields
    basenames, so the ref must be resolved by joining the path onto a model
    dir — not by matching the basename set."""
    models = tmp_path / "models"
    sub = models / "loras" / "wan2.2" / "_accel" / "i2v_lightx2v_model"
    sub.mkdir(parents=True)
    (sub / "i2v_lightx2v_low_noise_model.safetensors").write_bytes(b"x")
    monkeypatch.setattr(worker, "MODEL_DIRS", [str(models)])

    # workflow refs are relative to the loras dir (no leading "loras/")
    rel = "wan2.2/_accel/i2v_lightx2v_model/i2v_lightx2v_low_noise_model.safetensors"
    assert worker._resolve_model_path(rel) is not None
    # bare basename still resolves (legacy top-level refs)
    assert worker._resolve_model_path("i2v_lightx2v_low_noise_model.safetensors") is not None
    # genuinely absent ref still reports missing
    assert worker._resolve_model_path("wan2.2/nope/ghost.safetensors") is None


def test_check_models_exist_passes_for_subfolder_lora(worker, monkeypatch, tmp_path):
    models = tmp_path / "models"
    sub = models / "loras" / "wan2.2" / "_accel"
    sub.mkdir(parents=True)
    (sub / "wan2.2_animate_14B_relight_lora_bf16.safetensors").write_bytes(b"x")
    monkeypatch.setattr(worker, "MODEL_DIRS", [str(models)])

    wf = {"1": {"class_type": "LoraLoaderModelOnly",
                "inputs": {"lora_name": "wan2.2/_accel/wan2.2_animate_14B_relight_lora_bf16.safetensors"}}}
    missing, auto = worker._check_models_exist(wf)
    assert missing == []
