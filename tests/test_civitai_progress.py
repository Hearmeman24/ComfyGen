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

import io
import json
import os
import urllib.error

import pytest


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Stub the metadata lookup + _download_url so we can assert what
    _download_civitai hands off."""
    import download_handler

    seen: dict = {"url_calls": [], "metadata_calls": [], "resolve_calls": []}

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

    def fake_resolve_download_url(download_url, token=None):
        seen["resolve_calls"].append({"download_url": download_url, "token": token})
        return seen.get("resolved_download_url", download_url)

    monkeypatch.setattr(download_handler, "_civitai_version_metadata", fake_metadata)
    monkeypatch.setattr(
        download_handler,
        "_resolve_civitai_download_url",
        fake_resolve_download_url,
        raising=False,
    )
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


def test_civitai_token_resolves_redirect_but_is_not_passed_to_aria2c(patched, monkeypatch):
    seen, tmp_path = patched
    monkeypatch.setenv("CIVITAI_TOKEN", "secret-token-123")
    import download_handler

    seen["metadata_response"] = {
        "filename": "gated.safetensors",
        "sha256": "b" * 64,
        "download_url": "https://civitai.com/api/download/models/555",
    }
    seen["resolved_download_url"] = "https://b2.civitai.com/file/model.safetensors?Authorization=signed"

    download_handler._download_civitai("555", str(tmp_path))
    assert seen["resolve_calls"] == [{
        "download_url": "https://civitai.com/api/download/models/555",
        "token": "secret-token-123",
    }]
    call = seen["url_calls"][0]
    assert call["url"] == "https://b2.civitai.com/file/model.safetensors?Authorization=signed"
    assert call["extra_aria_args"] == []


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


def test_metadata_lookup_retries_transient_http_error(monkeypatch):
    import download_handler

    calls = {"count": 0}

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self):
            return json.dumps({
                "files": [{
                    "primary": True,
                    "name": "m.safetensors",
                    "hashes": {"SHA256": "a" * 64},
                    "downloadUrl": "https://civitai.com/api/download/models/1",
                }],
            }).encode()

    def fake_urlopen(_req, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(
                url="https://civitai.com/api/v1/model-versions/1",
                code=503,
                msg="Service Unavailable",
                hdrs={},
                fp=io.BytesIO(b"try again later"),
            )
        return _Resp()

    monkeypatch.setattr(download_handler.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(download_handler.time, "sleep", lambda _seconds: None)

    meta = download_handler._civitai_version_metadata("1", token="tok")

    assert calls["count"] == 2
    assert meta["filename"] == "m.safetensors"


def test_metadata_lookup_error_preserves_http_status_and_body(monkeypatch, tmp_path):
    import download_handler

    def fake_urlopen(_req, timeout):
        raise urllib.error.HTTPError(
            url="https://civitai.com/api/v1/model-versions/2960578",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b"token lacks access"),
        )

    monkeypatch.setattr(download_handler.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc:
        download_handler._download_civitai("2960578", str(tmp_path))

    msg = str(exc.value)
    assert "HTTP 403 Forbidden" in msg
    assert "token lacks access" in msg


def test_metadata_failure_uses_caller_filename_and_sha_fallback(monkeypatch, tmp_path):
    import download_handler

    seen = {}

    def fake_metadata(_version_id, token=None):
        raise download_handler.CivitaiMetadataError("HTTP 503 Service Unavailable")

    def fake_download_url(**kwargs):
        seen.update(kwargs)
        path = os.path.join(kwargs["dest_dir"], kwargs["filename"])
        os.makedirs(kwargs["dest_dir"], exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x")
        return {"filename": kwargs["filename"], "path": path, "size_mb": 0.0}

    monkeypatch.setattr(download_handler, "_civitai_version_metadata", fake_metadata)
    monkeypatch.setattr(download_handler, "_resolve_civitai_download_url", lambda url, token=None: url)
    monkeypatch.setattr(download_handler, "_download_url", fake_download_url)

    info = download_handler._download_civitai(
        "2960578",
        str(tmp_path),
        expected_sha="A" * 64,
        fallback_filename="mopPro_v21.safetensors",
    )

    assert info["filename"] == "mopPro_v21.safetensors"
    assert info["sha256"] == "a" * 64
    assert seen["url"] == "https://civitai.com/api/download/models/2960578"
    assert seen["expected_sha"] == "a" * 64
