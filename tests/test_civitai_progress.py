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

    # Block the API lookup so the test stays deterministic (and offline).
    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: None)

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
    # When the API lookup returns nothing, fall back to the abstract token.
    assert "civitai/2668710" in p["message"]
    assert "Downloading 3/8" in p["message"]
    assert 0 <= p["percent"] <= 100


def test_streams_via_progress_callback_for_sse(fake_subprocess, monkeypatch):
    import download_handler
    tmp_path, _ = fake_subprocess

    # API lookup returns the real filename + sha — but the sha won't match the
    # fake file's content, so dedup misses and we go through the subprocess.
    # This exercises the "filename in progress" path users actually see.
    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: {
                            "filename": "wan22EnhancedNSFWSVICamera.gguf",
                            "sha256": "a" * 64,
                        })

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
    # User-requested: surface the actual filename, not `civitai/<vid>`.
    assert progress_events[0]["file"] == "wan22EnhancedNSFWSVICamera.gguf"
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
    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: None)

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
    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: None)

    info = download_handler._download_civitai(
        version_id="555", dest_dir=str(tmp_path),
        job={"id": "diff-fallback"},
    )
    assert info["filename"] == "model.safetensors"


def test_dedup_skips_subprocess_when_existing_file_matches_api_sha(monkeypatch, tmp_path):
    """The big win: if a file with the API-reported SHA256 already exists
    in dest_dir, skip the multi-GB subprocess entirely.

    Also exercises the CPU-pod path implicitly — both routes (GPU /download
    and CPU install-preset) share download_handler.handle().
    """
    import download_handler

    target = tmp_path / "model.safetensors"
    payload = b"deterministic-bytes"
    target.write_bytes(payload)
    import hashlib
    sha = hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: {"filename": "model.safetensors", "sha256": sha})

    # Popen MUST NOT be called. Fail loudly if it is.
    def _no_popen(*a, **k):
        raise AssertionError("subprocess should be skipped when sha matches cached file")
    monkeypatch.setattr(download_handler.subprocess, "Popen", _no_popen)
    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: None)
                        })()}))

    info = download_handler._download_civitai(
        version_id="999", dest_dir=str(tmp_path),
        job={"id": "cached-1"},
    )
    assert info["cached"] is True
    assert info["filename"] == "model.safetensors"
    assert info["sha256"] == sha


def test_dedup_skips_when_only_renamed_match_exists(monkeypatch, tmp_path):
    """When the file exists at a DIFFERENT name than the API reports, we no
    longer scan-and-hash the whole directory looking for matches (bead cwt —
    that was costing minutes on populated network volumes). Dedup misses and
    the subprocess runs. The rare same-bytes-renamed case is sacrificed for
    predictable latency."""
    import download_handler
    import hashlib

    payload = b"same-bytes-different-name"
    sha = hashlib.sha256(payload).hexdigest()
    (tmp_path / "renamed_old.safetensors").write_bytes(payload)
    target = tmp_path / "new_name.safetensors"

    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: {"filename": "new_name.safetensors", "sha256": sha})

    class _Proc:
        returncode = 0
        def __init__(self):
            self.stdout = iter([f"Model ready at: {target}\n"])
        def wait(self, timeout=None):
            target.write_bytes(b"x" * (2 * 1024 * 1024))
            return 0
    monkeypatch.setattr(download_handler.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: None)
                        })()}))

    info = download_handler._download_civitai(
        version_id="999", dest_dir=str(tmp_path),
        job={"id": "cached-rename"},
    )
    assert info.get("cached") is not True
    assert info["filename"] == "new_name.safetensors"


def test_explicit_expected_sha_without_hint_skips_dedup(monkeypatch, tmp_path):
    """Caller-supplied expected_sha but no filename hint → we can't safely
    dedup without scanning, so we skip dedup and run the subprocess."""
    import download_handler
    import hashlib

    payload = b"explicit-sha-flow"
    sha = hashlib.sha256(payload).hexdigest()
    (tmp_path / "model.safetensors").write_bytes(payload)
    new_file = tmp_path / "model_v2.safetensors"

    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: None)

    class _Proc:
        returncode = 0
        def __init__(self):
            self.stdout = iter([f"Model ready at: {new_file}\n"])
        def wait(self, timeout=None):
            new_file.write_bytes(b"x" * (2 * 1024 * 1024))
            return 0
    monkeypatch.setattr(download_handler.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: None)
                        })()}))

    info = download_handler._download_civitai(
        version_id="999", dest_dir=str(tmp_path),
        job={"id": "explicit"}, expected_sha=sha,
    )
    assert info.get("cached") is not True


def test_find_file_by_sha_does_not_scan_whole_directory(tmp_path):
    """Critical perf guard: with no hint_name, the function must return
    immediately. With a hint_name that doesn't exist, also immediate.
    No hashing of unrelated files allowed (bead cwt)."""
    import download_handler

    for n in ("a.safetensors", "b.safetensors", "c.safetensors"):
        (tmp_path / n).write_bytes(b"x" * 64 * 1024)

    def _explode(*a, **k):
        raise AssertionError(f"_sha256_file called unexpectedly: {a}")
    orig = download_handler._sha256_file
    download_handler._sha256_file = _explode
    try:
        assert download_handler._find_file_by_sha(str(tmp_path), "deadbeef") is None
        assert download_handler._find_file_by_sha(str(tmp_path), "deadbeef", hint_name="nope.bin") is None
    finally:
        download_handler._sha256_file = orig


def test_civitai_filename_appears_in_progress_message_when_api_supplies_it(fake_subprocess, monkeypatch):
    """User-facing log fix: progress message says 'wan22Enhanced...gguf 45%',
    not 'civitai/2668710 45%'."""
    import download_handler
    tmp_path, _ = fake_subprocess

    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: {"filename": "wan22Enhanced.gguf", "sha256": "z" * 64})

    sent: list[dict] = []
    monkeypatch.setattr(download_handler, "runpod",
                        type("R", (), {"serverless": type("S", (), {
                            "progress_update": staticmethod(lambda j, p: sent.append(p))
                        })()}))

    download_handler._download_civitai(
        version_id="2668710", dest_dir=str(tmp_path),
        job={"id": "fname-disp"}, item_index=0, total_items=1,
    )
    assert sent
    msg = sent[0]["message"]
    assert "wan22Enhanced.gguf" in msg, msg
    assert "civitai/2668710" not in msg


def test_sha256_heartbeat_fires_on_long_hashes(monkeypatch, tmp_path, capsys):
    """Bead 8r7 — the post-download verify must not go silent on large files.
    Simulate elapsed time advancing 16s per chunk so the heartbeat (every 15s)
    fires multiple times during a single hash."""
    import download_handler

    target = tmp_path / "big.safetensors"
    target.write_bytes(b"x" * (256 * 1024))  # 4 chunks of 64 KiB

    t = [1000.0]
    def fake_time():
        t[0] += 16  # always past the 15s heartbeat threshold
        return t[0]
    monkeypatch.setattr(download_handler.time, "time", fake_time)

    digest = download_handler._sha256_file_with_heartbeat(
        str(target), job_tag="abc12345", label=target.name,
    )
    assert digest  # real hash returned
    out = capsys.readouterr().out
    assert "still hashing big.safetensors" in out
    assert "[job abc12345]" in out


def test_nonzero_exit_includes_log_tail(monkeypatch, tmp_path):
    import download_handler

    class _FailingProc:
        returncode = 2
        stdout = iter(["aria2c: ERR something exploded", ""])
        def wait(self, timeout=None): return 2

    monkeypatch.setattr(download_handler.subprocess, "Popen",
                        lambda *a, **k: _FailingProc())
    monkeypatch.setattr(download_handler, "_civitai_version_metadata",
                        lambda v, token=None: None)

    with pytest.raises(RuntimeError, match="exit 2") as exc:
        download_handler._download_civitai("999", str(tmp_path))
    assert "ERR something exploded" in str(exc.value)
