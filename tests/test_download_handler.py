"""Tests for download_handler sha256 verification and content-addressable dedup.

These tests cover the BlockFlow preset-installer contract: each download entry
may carry an expected sha256, and the handler must verify it post-download
(removing corrupt files), skip re-downloads when a matching file already exists,
and remain backwards-compatible when sha256 is absent.

aria2c is mocked at the subprocess boundary — we exercise the real verification
logic (hashlib) against real bytes on disk.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import download_handler


REAL_BYTES = b"hello world\n" * 1024  # 12 KiB of deterministic content
REAL_SHA = hashlib.sha256(REAL_BYTES).hexdigest()


@pytest.fixture
def models_base(tmp_path, monkeypatch):
    """Point download_handler at a temp MODELS_BASE for the duration of a test."""
    base = tmp_path / "models"
    base.mkdir()
    monkeypatch.setattr(download_handler, "MODELS_BASE", str(base))
    return base


@pytest.fixture
def fake_aria2c(mocker, models_base):
    """Replace subprocess.Popen used by aria2c with a fake that writes REAL_BYTES.

    Returns the mock so tests can assert call_count etc. The fake writes
    `REAL_BYTES` to the destination path computed from the Popen args; tests that
    want to simulate a corrupt download can override `payload`.
    """
    state = {"payload": REAL_BYTES, "returncode": 0, "calls": 0}

    class FakeProc:
        def __init__(self, argv, **_kw):
            state["calls"] += 1
            # argv looks like: aria2c -d <dir> -o <name> ... <url>
            dest_dir = argv[argv.index("-d") + 1]
            filename = argv[argv.index("-o") + 1]
            path = os.path.join(dest_dir, filename)
            os.makedirs(dest_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(state["payload"])
            # Popen interface bits used by _download_url
            self.stdout = iter([])
            self.returncode = state["returncode"]

        def wait(self, timeout=None):
            return self.returncode

    mocker.patch.object(download_handler.subprocess, "Popen", FakeProc)
    return state


def _job(downloads):
    return {"id": "test-job-xyz12345", "input": {"downloads": downloads}}


# --- backwards compatibility: no sha256 ---

def test_no_sha256_is_backwards_compatible(fake_aria2c, models_base):
    result = download_handler.handle(_job([
        {"source": "url", "url": "https://example.com/m.safetensors", "dest": "loras"},
    ]))
    assert result["ok"] is True
    assert len(result["files"]) == 1
    f = result["files"][0]
    assert f["filename"] == "m.safetensors"
    assert f["dest"] == "loras"
    assert Path(f["path"]).read_bytes() == REAL_BYTES


# --- sha256 verification path ---

def test_sha256_match_returns_verified_file(fake_aria2c, models_base):
    result = download_handler.handle(_job([
        {
            "source": "url",
            "url": "https://example.com/m.safetensors",
            "dest": "loras",
            "sha256": REAL_SHA,
        },
    ]))
    assert result["ok"] is True
    f = result["files"][0]
    assert f["sha256"] == REAL_SHA
    assert f["bytes"] == len(REAL_BYTES)
    assert f["cached"] is False
    assert os.path.isfile(f["path"])
    assert fake_aria2c["calls"] == 1


def test_sha256_mismatch_fails_and_removes_corrupt_file(fake_aria2c, models_base):
    bogus_expected = "0" * 64
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        download_handler.handle(_job([
            {
                "source": "url",
                "url": "https://example.com/m.safetensors",
                "dest": "loras",
                "sha256": bogus_expected,
            },
        ]))
    # File on disk must be cleaned up
    expected_path = models_base / "loras" / "m.safetensors"
    assert not expected_path.exists(), "corrupt file should be removed on mismatch"


# --- content-addressable dedup ---

def test_preexisting_matching_file_skips_download(fake_aria2c, models_base):
    # Plant a file with the right hash before calling the handler
    dest = models_base / "loras"
    dest.mkdir()
    (dest / "m.safetensors").write_bytes(REAL_BYTES)

    result = download_handler.handle(_job([
        {
            "source": "url",
            "url": "https://example.com/m.safetensors",
            "dest": "loras",
            "sha256": REAL_SHA,
        },
    ]))
    assert result["ok"] is True
    f = result["files"][0]
    assert f["cached"] is True
    assert f["sha256"] == REAL_SHA
    assert fake_aria2c["calls"] == 0, "aria2c must NOT be called when file already matches"


def test_preexisting_file_wrong_hash_triggers_redownload(fake_aria2c, models_base):
    # Plant a file whose hash does NOT match expected — handler should re-download
    dest = models_base / "loras"
    dest.mkdir()
    (dest / "m.safetensors").write_bytes(b"stale junk")

    result = download_handler.handle(_job([
        {
            "source": "url",
            "url": "https://example.com/m.safetensors",
            "dest": "loras",
            "sha256": REAL_SHA,
        },
    ]))
    assert result["ok"] is True
    assert result["files"][0]["cached"] is False
    assert result["files"][0]["sha256"] == REAL_SHA
    assert fake_aria2c["calls"] == 1


# --- destination_path synonym ---

def test_destination_path_synonym(fake_aria2c, models_base):
    result = download_handler.handle(_job([
        {
            "source": "url",
            "url": "https://example.com/ignored.bin",
            "destination_path": "loras/sub/myfile.safetensors",
            "sha256": REAL_SHA,
        },
    ]))
    assert result["ok"] is True
    f = result["files"][0]
    assert f["path"] == str(models_base / "loras" / "sub" / "myfile.safetensors")
    assert f["filename"] == "myfile.safetensors"
    assert f["dest"] == "loras/sub"
    assert os.path.isfile(f["path"])


def test_destination_path_dedup_also_works(fake_aria2c, models_base):
    target = models_base / "loras" / "sub" / "myfile.safetensors"
    target.parent.mkdir(parents=True)
    target.write_bytes(REAL_BYTES)

    result = download_handler.handle(_job([
        {
            "source": "url",
            "url": "https://example.com/ignored.bin",
            "destination_path": "loras/sub/myfile.safetensors",
            "sha256": REAL_SHA,
        },
    ]))
    assert result["files"][0]["cached"] is True
    assert fake_aria2c["calls"] == 0


# --- edge cases ---

def test_empty_downloads_payload_raises(fake_aria2c, models_base):
    with pytest.raises(RuntimeError, match="No downloads"):
        download_handler.handle(_job([]))


def test_unknown_source_raises(fake_aria2c, models_base):
    with pytest.raises(RuntimeError, match="unknown source"):
        download_handler.handle(_job([{"source": "ftp", "url": "x"}]))


def test_url_source_missing_url_raises(fake_aria2c, models_base):
    with pytest.raises(RuntimeError, match="'url' required"):
        download_handler.handle(_job([{"source": "url", "dest": "loras"}]))


def test_partial_failure_second_entry_mismatch_removes_only_bad_file(
    fake_aria2c, models_base,
):
    # First entry: legit. Second entry: hash mismatch → must remove second file
    # and raise; first file remains on disk (consistent with current fail-fast
    # behavior of the handler).
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        download_handler.handle(_job([
            {
                "source": "url",
                "url": "https://example.com/a.safetensors",
                "dest": "loras",
                "sha256": REAL_SHA,
            },
            {
                "source": "url",
                "url": "https://example.com/b.safetensors",
                "dest": "loras",
                "sha256": "0" * 64,
            },
        ]))
    assert (models_base / "loras" / "a.safetensors").exists()
    assert not (models_base / "loras" / "b.safetensors").exists()
