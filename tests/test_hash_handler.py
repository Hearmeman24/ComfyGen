"""Tests for the `hash` worker command.

The handler streams sha256 of files already on the network volume so callers
can decide whether to skip a download. Mirrors delete_handler's security model:
realpath-resolved paths only, anything outside VOLUME_ROOT is rejected.
"""

from __future__ import annotations

import hashlib
import os

import pytest


KNOWN_BYTES = b"the quick brown fox jumps over the lazy dog"
KNOWN_SHA256 = hashlib.sha256(KNOWN_BYTES).hexdigest()


@pytest.fixture
def fake_volume(tmp_path, monkeypatch):
    """Point hash_handler.VOLUME_ROOT at a tmp dir so tests touch real files."""
    import hash_handler
    root = tmp_path / "runpod-volume"
    root.mkdir()
    monkeypatch.setattr(hash_handler, "VOLUME_ROOT", str(root))
    return root


def _write(path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def test_hash_returns_sha256_and_bytes_for_existing_file(fake_volume, dispatch_command):
    p = _write(fake_volume / "models" / "loras" / "m.safetensors", KNOWN_BYTES)
    res = dispatch_command({"command": "hash", "paths": [p]})
    assert res["ok"] is True
    assert res["files"] == [{"path": p, "sha256": KNOWN_SHA256, "bytes": len(KNOWN_BYTES)}]


def test_hash_multiple_files_returns_results_in_order(fake_volume, dispatch_command):
    a = _write(fake_volume / "a.bin", b"AAA")
    b = _write(fake_volume / "b.bin", b"BBBB")
    res = dispatch_command({"command": "hash", "paths": [a, b]})
    assert [f["path"] for f in res["files"]] == [a, b]
    assert res["files"][0]["bytes"] == 3
    assert res["files"][1]["bytes"] == 4


def test_hash_missing_file_returns_per_path_error(fake_volume, dispatch_command):
    missing = str(fake_volume / "nope.bin")
    res = dispatch_command({"command": "hash", "paths": [missing]})
    assert res["ok"] is True  # batch ok; per-file error
    assert res["files"][0]["path"] == missing
    assert res["files"][0]["sha256"] is None
    assert res["files"][0]["error"] == "not found"


def test_hash_rejects_path_outside_volume(fake_volume, dispatch_command):
    res = dispatch_command({"command": "hash", "paths": ["/etc/passwd"]})
    assert res["files"][0]["sha256"] is None
    assert "outside" in res["files"][0]["error"]


def test_hash_rejects_dotdot_traversal(fake_volume, dispatch_command, tmp_path):
    # Path that lexically lives under the volume but resolves outside it.
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"x")
    traversal = str(fake_volume / ".." / "secret.txt")
    res = dispatch_command({"command": "hash", "paths": [traversal]})
    assert res["files"][0]["sha256"] is None
    assert "outside" in res["files"][0]["error"]


def test_hash_rejects_symlink_escape(fake_volume, dispatch_command, tmp_path):
    target = tmp_path / "outside.bin"
    target.write_bytes(b"secret")
    link = fake_volume / "link.bin"
    os.symlink(str(target), str(link))
    res = dispatch_command({"command": "hash", "paths": [str(link)]})
    assert res["files"][0]["sha256"] is None
    assert "outside" in res["files"][0]["error"]
    # And the symlinked target was not opened: file still present unchanged.
    assert target.read_bytes() == b"secret"


def test_hash_directory_path_returns_error(fake_volume, dispatch_command):
    d = fake_volume / "models"
    d.mkdir()
    res = dispatch_command({"command": "hash", "paths": [str(d)]})
    assert res["files"][0]["sha256"] is None
    assert "not a file" in res["files"][0]["error"]


def test_hash_empty_paths_list_returns_empty_files(fake_volume, dispatch_command):
    res = dispatch_command({"command": "hash", "paths": []})
    assert res == {"ok": True, "files": []}


def test_hash_missing_paths_field(fake_volume, dispatch_command):
    res = dispatch_command({"command": "hash"})
    assert res["ok"] is False
    assert "paths" in res["error"]


def test_hash_paths_not_a_list(fake_volume, dispatch_command):
    res = dispatch_command({"command": "hash", "paths": "not-a-list"})
    assert res["ok"] is False
    assert "list" in res["error"]


def test_hash_streams_large_file_without_loading_into_memory(fake_volume, dispatch_command):
    # 5 MiB of repeating bytes — bigger than the 64 KiB hashing chunk.
    big = fake_volume / "big.bin"
    chunk = b"A" * (64 * 1024)
    with big.open("wb") as f:
        for _ in range(80):
            f.write(chunk)
    expected = hashlib.sha256(chunk * 80).hexdigest()
    res = dispatch_command({"command": "hash", "paths": [str(big)]})
    assert res["files"][0]["sha256"] == expected
    assert res["files"][0]["bytes"] == 80 * 64 * 1024


def test_hash_mixed_batch_continues_past_errors(fake_volume, dispatch_command):
    good = _write(fake_volume / "ok.bin", KNOWN_BYTES)
    missing = str(fake_volume / "nope.bin")
    outside = "/etc/passwd"
    res = dispatch_command({"command": "hash", "paths": [good, missing, outside]})
    assert res["ok"] is True
    assert res["files"][0]["sha256"] == KNOWN_SHA256
    assert res["files"][1]["error"] == "not found"
    assert "outside" in res["files"][2]["error"]
