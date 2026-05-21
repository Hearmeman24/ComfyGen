"""Tests for the worker-level /delete command.

The /delete command removes model files from the network volume to support
BlockFlow's preset uninstall flow. Security-critical: paths outside
`/runpod-volume` MUST be rejected (no traversal, no symlink escapes), and
missing files MUST be idempotent (no exception).
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def fake_volume(tmp_path, monkeypatch):
    """Create a fake /runpod-volume root and point delete_handler at it."""
    import delete_handler

    volume = tmp_path / "runpod-volume"
    volume.mkdir()
    monkeypatch.setattr(delete_handler, "VOLUME_ROOT", str(volume))
    return volume


def test_delete_removes_existing_file(dispatch_command, fake_volume):
    target = fake_volume / "ComfyUI" / "models" / "loras" / "old.safetensors"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 16)

    result = dispatch_command({"command": "delete", "paths": [str(target)]})

    assert result == {
        "ok": True,
        "results": [{"path": str(target), "deleted": True}],
    }
    assert not target.exists()


def test_delete_missing_file_is_idempotent(dispatch_command, fake_volume):
    ghost = fake_volume / "ComfyUI" / "models" / "loras" / "ghost.safetensors"

    result = dispatch_command({"command": "delete", "paths": [str(ghost)]})

    assert result["ok"] is True
    assert result["results"] == [
        {"path": str(ghost), "deleted": False, "error": "not found"}
    ]


def test_delete_rejects_path_outside_volume(dispatch_command, fake_volume, tmp_path):
    outside = tmp_path / "evil.txt"
    outside.write_text("secret")

    result = dispatch_command({"command": "delete", "paths": [str(outside)]})

    assert result["ok"] is True
    entry = result["results"][0]
    assert entry["path"] == str(outside)
    assert entry["deleted"] is False
    assert "outside" in entry["error"].lower()
    assert outside.exists(), "file outside volume must NEVER be deleted"


def test_delete_rejects_etc_passwd(dispatch_command, fake_volume):
    result = dispatch_command({"command": "delete", "paths": ["/etc/passwd"]})

    assert result["results"][0]["deleted"] is False
    assert "outside" in result["results"][0]["error"].lower()


def test_delete_rejects_dotdot_traversal(dispatch_command, fake_volume, tmp_path):
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("data")
    # Path is *textually* under VOLUME_ROOT but resolves outside via `..`.
    traversal = str(fake_volume / ".." / "sibling.txt")

    result = dispatch_command({"command": "delete", "paths": [traversal]})

    assert result["results"][0]["deleted"] is False
    assert "outside" in result["results"][0]["error"].lower()
    assert sibling.exists()


def test_delete_rejects_symlink_escape(dispatch_command, fake_volume, tmp_path):
    outside = tmp_path / "outside-target.txt"
    outside.write_text("important")
    link = fake_volume / "escape.lnk"
    os.symlink(str(outside), str(link))

    result = dispatch_command({"command": "delete", "paths": [str(link)]})

    assert result["results"][0]["deleted"] is False
    assert "outside" in result["results"][0]["error"].lower()
    assert outside.exists(), "symlink target outside volume must not be deleted"


def test_delete_multiple_paths_per_path_results(dispatch_command, fake_volume):
    keep = fake_volume / "a.safetensors"
    keep.write_bytes(b"a")
    gone = fake_volume / "b.safetensors"
    gone.write_bytes(b"b")
    missing = fake_volume / "missing.safetensors"

    result = dispatch_command(
        {"command": "delete", "paths": [str(keep), str(gone), str(missing), "/etc/hosts"]}
    )

    assert result["ok"] is True
    assert len(result["results"]) == 4
    by_path = {r["path"]: r for r in result["results"]}
    assert by_path[str(keep)]["deleted"] is True
    assert by_path[str(gone)]["deleted"] is True
    assert by_path[str(missing)] == {
        "path": str(missing),
        "deleted": False,
        "error": "not found",
    }
    assert by_path["/etc/hosts"]["deleted"] is False
    assert "outside" in by_path["/etc/hosts"]["error"].lower()
    assert not keep.exists()
    assert not gone.exists()


def test_delete_empty_paths_list(dispatch_command, fake_volume):
    result = dispatch_command({"command": "delete", "paths": []})
    assert result == {"ok": True, "results": []}


def test_delete_paths_not_a_list_errors(dispatch_command, fake_volume):
    result = dispatch_command({"command": "delete", "paths": "not-a-list"})
    assert result["ok"] is False
    assert "paths" in result["error"].lower()


def test_delete_missing_paths_field_errors(dispatch_command, fake_volume):
    result = dispatch_command({"command": "delete"})
    assert result["ok"] is False
    assert "paths" in result["error"].lower()
