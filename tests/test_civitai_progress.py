"""Tests for _download_civitai under the post-746a21e direct-aria2c
architecture.

The wrapped Hearmeman24/CivitAI_Downloader subprocess has been replaced with a
thin wrapper that:
  1. Calls the CivitAI API to resolve filename + sha256 + downloadUrl.
  2. Checks dedup (existing file at hint name with matching sha → cached).
  3. Otherwise hands off to _download_url with --checksum for in-flight verify.

No subprocess streaming, no async verify, no Model-ready-at parsing — the
file is verified by aria2c during the download, same as URL/HF.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Stub the metadata lookup + _download_url so we can assert what
    _download_civitai hands off."""
    import download_handler

    seen: dict = {"url_calls": [], "metadata_calls": []}

    def fake_metadata(version_id, token=None):
        seen["metadata_calls"].append({"version_id": version_id, "token": token})
        return seen.get("metadata_response")

    def fake_download_url(**kwargs):
        seen["url_calls"].append(kwargs)
        path = os.path.join(kwargs["dest_dir"], kwargs["filename"])
        os.makedirs(kwargs["dest_dir"], exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024))
        return {
            "filename": kwargs["filename"],
            "path": path,
            "size_mb": 2.0,
            "dest": "",
            "cached": False,
            "bytes": 2 * 1024 * 1024,
        }

    monkeypatch.setattr(download_handler, "_civitai_version_metadata", fake_metadata)
    monkeypatch.setattr(download_handler, "_download_url", fake_download_url)
    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: None)
                        })()}))
    return seen, tmp_path


def test_metadata_lookup_failure_raises_clean_error(patched):
    seen, tmp_path = patched
    seen["metadata_response"] = None
    import download_handler
    with pytest.raises(RuntimeError, match="metadata lookup failed for version 9999"):
        download_handler._download_civitai("9999", str(tmp_path))


def test_dedup_hit_returns_cached_without_calling_download_url(patched, monkeypatch):
    seen, tmp_path = patched
    import hashlib
    import download_handler

    payload = b"some-bytes"
    sha = hashlib.sha256(payload).hexdigest()
    (tmp_path / "mopPro_v21.safetensors").write_bytes(payload)

    seen["metadata_response"] = {
        "filename": "mopPro_v21.safetensors",
        "sha256": sha,
        "download_url": "https://civitai.com/api/download/models/2960578",
    }

    info = download_handler._download_civitai(
        "2960578", str(tmp_path), job={"id": "test-dedup"},
    )
    assert info["cached"] is True
    assert info["sha256"] == sha
    assert info["filename"] == "mopPro_v21.safetensors"
    assert seen["url_calls"] == [], "_download_url must NOT be called on cache hit"


def test_cache_miss_hands_off_to_download_url_with_checksum(patched):
    seen, tmp_path = patched
    import download_handler

    seen["metadata_response"] = {
        "filename": "model.safetensors",
        "sha256": "a" * 64,
        "download_url": "https://civitai.com/api/download/models/123",
    }

    info = download_handler._download_civitai(
        "123", str(tmp_path), job={"id": "test-miss"},
        item_index=0, total_items=1,
    )
    assert seen["url_calls"], "_download_url must be called on cache miss"
    call = seen["url_calls"][0]
    assert call["url"] == "https://civitai.com/api/download/models/123"
    assert call["filename"] == "model.safetensors"
    assert call["expected_sha"] == "a" * 64
    # info gets the verified sha stamped on.
    assert info["sha256"] == "a" * 64


def test_civitai_token_passed_as_auth_header_to_aria2c(patched, monkeypatch):
    seen, tmp_path = patched
    monkeypatch.setenv("CIVITAI_TOKEN", "secret-token-123")
    import download_handler

    seen["metadata_response"] = {
        "filename": "gated.safetensors",
        "sha256": "b" * 64,
        "download_url": "https://civitai.com/api/download/models/555",
    }

    download_handler._download_civitai("555", str(tmp_path))
    call = seen["url_calls"][0]
    assert call["extra_aria_args"] == ["--header=Authorization: Bearer secret-token-123"]


def test_no_token_means_no_auth_header(patched, monkeypatch):
    seen, tmp_path = patched
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    import download_handler

    seen["metadata_response"] = {
        "filename": "public.safetensors",
        "sha256": "c" * 64,
        "download_url": "https://civitai.com/api/download/models/777",
    }

    download_handler._download_civitai("777", str(tmp_path))
    call = seen["url_calls"][0]
    assert call["extra_aria_args"] == []


def test_caller_expected_sha_overrides_api_sha(patched):
    seen, tmp_path = patched
    import download_handler

    seen["metadata_response"] = {
        "filename": "model.safetensors",
        "sha256": "aaaa" * 16,  # what the API says
        "download_url": "https://civitai.com/api/download/models/999",
    }

    download_handler._download_civitai(
        "999", str(tmp_path), expected_sha="BBBB" * 16,
    )
    call = seen["url_calls"][0]
    # Caller-supplied sha wins, lowercased.
    assert call["expected_sha"] == "bbbb" * 16


def test_metadata_lookup_token_forwarded(patched, monkeypatch):
    """The CIVITAI_TOKEN env var must reach the API call so gated metadata
    works on the first try."""
    seen, tmp_path = patched
    monkeypatch.setenv("CIVITAI_TOKEN", "api-token-abc")
    import download_handler

    seen["metadata_response"] = {
        "filename": "x.safetensors",
        "sha256": "d" * 64,
        "download_url": "https://civitai.com/api/download/models/1",
    }
    download_handler._download_civitai("1", str(tmp_path))
    assert seen["metadata_calls"][0]["token"] == "api-token-abc"
