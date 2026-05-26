"""Tests for live progress streaming on CivitAI downloads.

Bead remote_comfy_generator-poo. Multi-GB CivitAI downloads were silent for
their full duration because `_download_civitai` used `subprocess.run(
capture_output=True)` which buffers everything until exit. Refactored to
Popen+line-stream and to emit `runpod.serverless.progress_update` events
and SSE `download_progress` callbacks mid-download.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fake_subprocess(monkeypatch, tmp_path):
    """Fake the CivitAI subprocess: emit canned aria2c-style progress lines
    on stdout, then exit 0 after touching a model file."""
    import download_handler

    aria_lines = [
        "Resolving CivitAI version 2668710 ...",
        "[#abc 0B/3.5GiB(0%) CN:1 DL:0B]",
        "[#abc 1.0GiB/3.5GiB(28%) CN:8 DL:52MiB]",
        "[#abc 2.5GiB/3.5GiB(71%) CN:8 DL:48MiB]",
        "[#abc 3.5GiB/3.5GiB(100%) CN:8 DL:50MiB]",
        "Download complete.",
    ]

    expected_file = tmp_path / "civitai_model.safetensors"

    class _FakeProc:
        def __init__(self, lines, dest_dir, target_file):
            self._lines = iter(lines)
            self._dest = Path(dest_dir)
            self._target = Path(target_file)
            self.returncode = 0
            self.stdout = self  # iterable

        def __iter__(self):
            return self

        def __next__(self):
            try:
                line = next(self._lines)
            except StopIteration:
                # First time we run out, drop the model file so the
                # post-subprocess "new files" detection finds it.
                if not self._target.exists():
                    self._target.write_bytes(b"x" * 1024)
                raise
            return line + "\n"

        def wait(self, timeout=None):
            # Drain any remaining lines and ensure the file exists.
            for _ in self._lines:
                pass
            if not self._target.exists():
                self._target.write_bytes(b"x" * 1024)
            return self.returncode

    def fake_popen(cmd, **kwargs):
        # The cmd passes `-o <dest_dir>`. Pull it out so we drop the fake file
        # under the right path.
        dest_dir = cmd[cmd.index("-o") + 1]
        # delay 3-second-throttle gates by patching time.time so we see at
        # least one progress event in the stream
        return _FakeProc(aria_lines, dest_dir, expected_file)

    monkeypatch.setattr(download_handler.subprocess, "Popen", fake_popen)

    # Force the throttle gates open by making "time" advance by 4s per call.
    t = [1000.0]
    def fake_time():
        t[0] += 4
        return t[0]
    monkeypatch.setattr(download_handler.time, "time", fake_time)

    return tmp_path, expected_file


def test_streams_aria2c_progress_via_runpod_progress_update(fake_subprocess, monkeypatch):
    import download_handler
    tmp_path, _ = fake_subprocess

    sent: list[dict] = []

    class _FakeRunpod:
        class serverless:
            @staticmethod
            def progress_update(job, payload):
                sent.append(payload)

    monkeypatch.setattr(download_handler, "runpod", _FakeRunpod)

    download_handler._download_civitai(
        version_id="2668710",
        dest_dir=str(tmp_path),
        job={"id": "test-job-civi-001"},
        item_index=2,
        total_items=8,
    )

    # At least one IN_PROGRESS payload must surface mid-stream.
    assert sent, "expected at least one progress_update during the stream"
    p = sent[0]
    assert p["stage"] == "download"
    assert "civitai/2668710" in p["message"]
    assert "Downloading 3/8" in p["message"]
    assert 0 <= p["percent"] <= 100


def test_streams_via_progress_callback_for_sse(fake_subprocess, monkeypatch):
    import download_handler
    tmp_path, _ = fake_subprocess

    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: None)
                        })()}))

    events: list[dict] = []
    download_handler._download_civitai(
        version_id="2668710",
        dest_dir=str(tmp_path),
        job={"id": "test-job-civi-002"},
        item_index=0,
        total_items=1,
        progress_callback=events.append,
    )

    progress_events = [e for e in events if e["type"] == "download_progress"]
    assert progress_events, "expected at least one download_progress SSE event"
    assert progress_events[0]["file"] == "civitai/2668710"
    assert 0 <= progress_events[0]["percent"] <= 100


def test_parses_model_ready_at_line_even_when_dest_had_prior_files(monkeypatch, tmp_path):
    """Regression: a prior failed install can leave files in the dest dir.
    aria2c resumes/overwrites in place, so `after - before` is empty even
    though the download succeeded. We must use the script's
    'Model ready at: <path>' line as the authoritative answer."""
    import download_handler

    # Simulate "prior attempt" debris: the target file already exists.
    stale = tmp_path / "wan22EnhancedNSFWSVICamera_nolightningSVICfQ8H.gguf"
    stale.write_bytes(b"old content")
    (tmp_path / "wan22EnhancedNSFWSVICamera_nolightningSVICfQ8L.gguf.aria2").write_bytes(b"partial")
    (tmp_path / "put_unet_files_here").write_bytes(b"marker")

    lines = [
        "[#4face3 14GiB/14GiB(100%) CN:8 DL:291MiB]",
        "Download Results:",
        "4face3|OK  |   291MiB/s|100|" + str(stale),
        "Status Legend: (OK):download completed.",
        "🔍 Checking for downloaded files...",
        "✅ Download complete: File valid (14692.9MB)",
        "🔍 Processing file: wan22EnhancedNSFWSVICamera_nolightningSVICfQ8H.gguf",
        f"✅ Model ready at: {stale}",
    ]

    class _Proc:
        returncode = 0
        def __init__(self):
            self.stdout = (l + "\n" for l in lines)
        def wait(self, timeout=None):
            # Refresh mtime to simulate aria2c rewriting the file
            stale.write_bytes(b"x" * (2 * 1024 * 1024))
            return 0

    monkeypatch.setattr(download_handler.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: None)
                        })()}))

    info = download_handler._download_civitai(
        version_id="2668710", dest_dir=str(tmp_path),
        job={"id": "regression-15u"},
    )

    assert info["filename"] == stale.name
    assert info["path"] == str(stale)
    assert info["size_mb"] > 0


def test_aria2c_partial_files_are_ignored_in_diff_fallback(monkeypatch, tmp_path):
    """When the 'Model ready at:' marker is absent, the diff fallback must
    ignore .aria2 partial-state files — picking one would return a path the
    caller can't actually use."""
    import download_handler

    # No prior files: dir starts empty.
    real_file = tmp_path / "model.safetensors"

    lines = [
        "[#abc 100%]",
        "Download Results:",
        # No "Model ready at:" line — forces the diff fallback path.
    ]

    class _Proc:
        returncode = 0
        def __init__(self):
            self.stdout = (l + "\n" for l in lines)
        def wait(self, timeout=None):
            real_file.write_bytes(b"x" * 1024)
            # Also drop a stray .aria2 partial; the picker must skip it.
            (tmp_path / "model.safetensors.aria2").write_bytes(b"")
            return 0

    monkeypatch.setattr(download_handler.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: None)
                        })()}))

    info = download_handler._download_civitai(
        version_id="555", dest_dir=str(tmp_path),
        job={"id": "diff-fallback"},
    )
    assert info["filename"] == "model.safetensors"


def test_nonzero_exit_includes_log_tail(monkeypatch, tmp_path):
    import download_handler

    class _FailingProc:
        returncode = 2
        stdout = iter(["aria2c: ERR something exploded", ""])
        def wait(self, timeout=None): return 2

    monkeypatch.setattr(download_handler.subprocess, "Popen",
                        lambda *a, **k: _FailingProc())

    with pytest.raises(RuntimeError, match="exit 2") as exc:
        download_handler._download_civitai("999", str(tmp_path))
    assert "ERR something exploded" in str(exc.value)
