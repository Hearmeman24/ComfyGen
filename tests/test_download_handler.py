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
import io
import json
import os
import subprocess
import sys
import time
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
            # argv looks like: aria2c -d <dir> -o <name> ... [--checksum=sha-256=<hex>] <url>
            dest_dir = argv[argv.index("-d") + 1]
            filename = argv[argv.index("-o") + 1]
            path = os.path.join(dest_dir, filename)
            os.makedirs(dest_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(state["payload"])

            # Emulate aria2c --checksum=sha-256=<hex> in-flight verification:
            # if the supplied checksum doesn't match what we just wrote, exit
            # non-zero (caller deletes the corrupt file). Mirrors real aria2c.
            import hashlib
            self.returncode = state["returncode"]
            checksum_arg = next(
                (a for a in argv if a.startswith("--checksum=sha-256=")), None,
            )
            if checksum_arg:
                expected = checksum_arg.split("=", 2)[2].lower()
                actual = hashlib.sha256(state["payload"]).hexdigest()
                if expected != actual:
                    self.returncode = 32  # aria2c's exit code on checksum fail
            self.stdout = iter([])

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


def test_huggingface_source_aliases_url(fake_aria2c, models_base):
    """`source: "huggingface"` is functionally identical to `source: "url"` —
    aria2c against the given URL. The blockflow-presets schema emits it; the
    handler must accept it."""
    result = download_handler.handle(_job([
        {"source": "huggingface",
         "url": "https://huggingface.co/m.safetensors",
         "dest": "loras",
         "sha256": REAL_SHA},
    ]))
    assert result["ok"] is True
    f = result["files"][0]
    assert f["filename"] == "m.safetensors"
    assert f["sha256"] == REAL_SHA
    assert f["cached"] is False
    assert fake_aria2c["calls"] == 1


def test_url_sha256_skips_post_download_rehash(fake_aria2c, models_base, mocker):
    """With expected_sha + aria2c --checksum, the post-download _sha256_file
    re-hash must not run — aria2c verifies in-flight, that's the whole win."""
    spy = mocker.spy(download_handler, "_sha256_file")
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
    assert f["sha256"] == REAL_SHA.lower()
    # Critical: zero post-download hash calls when aria2c handles --checksum.
    assert spy.call_count == 0


def test_url_passes_expected_sha_to_aria2c_checksum_flag(fake_aria2c, models_base, mocker):
    """The --checksum=sha-256=<hex> arg must reach the subprocess."""
    seen_argv = []
    real_proc_class = mocker.patch.object(
        download_handler.subprocess, "Popen",
        side_effect=lambda argv, **k: (seen_argv.append(argv) or _PassthroughProc(argv)),
    )

    class _PassthroughProc:
        def __init__(self, argv):
            dest_dir = argv[argv.index("-d") + 1]
            filename = argv[argv.index("-o") + 1]
            path = os.path.join(dest_dir, filename)
            os.makedirs(dest_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(REAL_BYTES)
            self.stdout = iter([])
            self.returncode = 0
        def wait(self, timeout=None):
            return 0

    # Re-bind the closure after defining the class
    real_proc_class.side_effect = lambda argv, **k: (seen_argv.append(argv) or _PassthroughProc(argv))

    download_handler.handle(_job([
        {"source": "url", "url": "https://example.com/m.safetensors",
         "dest": "loras", "sha256": REAL_SHA},
    ]))
    assert seen_argv, "Popen must have been called"
    flat = " ".join(seen_argv[0])
    assert f"--checksum=sha-256={REAL_SHA.lower()}" in flat


def test_dest_with_file_path_is_split_defensively(fake_aria2c, models_base):
    """If a caller passes the full file path in `dest` (foot-gun seen in the
    wild from BlockFlow's GPU-fallback installer), the handler defensively
    splits it the same way `destination_path` would. Without this, makedirs
    explodes on the existing file at that path."""
    target = models_base / "text_encoders" / "foo.safetensors"
    target.parent.mkdir(parents=True)
    target.write_bytes(REAL_BYTES)
    result = download_handler.handle(_job([
        {"source": "url",
         "url": "https://example.com/foo.safetensors",
         "dest": "text_encoders/foo.safetensors",
         "sha256": REAL_SHA},
    ]))
    assert result["ok"] is True
    f = result["files"][0]
    assert f["cached"] is True
    assert f["dest"] == "text_encoders"
    assert f["filename"] == "foo.safetensors"
    assert fake_aria2c["calls"] == 0


def test_dest_subfolder_only_still_works(fake_aria2c, models_base):
    """Regression: `dest` as a plain subfolder must keep working (the canonical
    API shape). The defensive normalization only kicks in when `dest` looks
    like a file path."""
    result = download_handler.handle(_job([
        {"source": "url",
         "url": "https://example.com/m.safetensors",
         "dest": "loras",
         "sha256": REAL_SHA},
    ]))
    assert result["ok"] is True
    assert result["files"][0]["dest"] == "loras"
    assert result["files"][0]["filename"] == "m.safetensors"


def test_dest_with_explicit_filename_skips_normalization(fake_aria2c, models_base):
    """If caller passes BOTH `dest` (with slashes) AND `filename` explicitly,
    trust them — they knew what they were doing. No defensive split."""
    result = download_handler.handle(_job([
        {"source": "url",
         "url": "https://example.com/m.safetensors",
         "dest": "loras/sub",
         "filename": "m.safetensors",
         "sha256": REAL_SHA},
    ]))
    assert result["ok"] is True
    f = result["files"][0]
    assert f["dest"] == "loras/sub"
    assert f["filename"] == "m.safetensors"


def test_huggingface_source_dedup_matches_url_path(fake_aria2c, models_base):
    """Pre-existing file with matching sha256 must skip aria2c regardless of
    whether the source is 'url' or 'huggingface'."""
    dest = models_base / "loras"
    dest.mkdir()
    (dest / "m.safetensors").write_bytes(REAL_BYTES)
    result = download_handler.handle(_job([
        {"source": "huggingface",
         "url": "https://huggingface.co/m.safetensors",
         "dest": "loras",
         "sha256": REAL_SHA},
    ]))
    assert result["ok"] is True
    assert result["files"][0]["cached"] is True
    assert fake_aria2c["calls"] == 0


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


# --- CLI entrypoint (_cli_main) — used by the CPU installer pod ---
#
# The installer pod runs `python -m download_handler --job /tmp/job.json`
# instead of the runpod harness. The CLI must read a job dict (same shape as
# the worker dispatch input), invoke handle(), print the result as JSON to
# stdout, and exit 0 iff result["ok"] is truthy.

def test_cli_main_reads_stdin_and_prints_result(monkeypatch, capsys, mocker):
    job = {"input": {"command": "download", "downloads": [{"source": "url"}]}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(job)))
    fake_handle = mocker.patch.object(
        download_handler, "handle", return_value={"ok": True, "files": []}
    )
    rc = download_handler._cli_main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == {"ok": True, "files": []}
    fake_handle.assert_called_once_with(job)


def test_cli_main_reads_job_file(tmp_path, capsys, mocker):
    job = {"input": {"command": "download", "downloads": [{"source": "url"}]}}
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job))
    mocker.patch.object(download_handler, "handle", return_value={"ok": True, "files": []})
    rc = download_handler._cli_main(["--job", str(job_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == {"ok": True, "files": []}


def test_cli_main_exits_1_when_handle_returns_not_ok(monkeypatch, capsys, mocker):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"input": {}})))
    mocker.patch.object(download_handler, "handle", return_value={"ok": False, "error": "x"})
    rc = download_handler._cli_main([])
    assert rc == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "x"}


def test_cli_main_malformed_json_raises(monkeypatch, mocker):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json {"))
    mocker.patch.object(download_handler, "handle")
    with pytest.raises(json.JSONDecodeError):
        download_handler._cli_main([])


def test_send_progress_is_noop_without_runpod_harness(monkeypatch):
    # When the CLI runs the installer pod, runpod.serverless.progress_update has
    # no live job context. The existing try/except must swallow whatever it
    # raises so download work continues.
    def boom(*_a, **_kw):
        raise RuntimeError("no harness")
    monkeypatch.setattr(download_handler.runpod.serverless, "progress_update", boom)
    download_handler._send_progress({"id": "x"}, "msg", 50.0)  # must not raise


def test_stream_process_output_kills_silent_process_on_timeout():
    """A silent child must not trap the downloader in a blocking stdout loop."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        download_handler._stream_process_output(proc, 0.2, lambda _line: None)

    assert time.monotonic() - started < 2.0
    assert proc.poll() is not None


def test_download_url_times_out_silent_aria2c(monkeypatch, tmp_path):
    """_download_url must turn a silent aria2c hang into a bounded failure."""
    real_popen = subprocess.Popen

    def fake_popen(_argv, **kwargs):
        return real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **kwargs,
        )

    monkeypatch.setattr(download_handler.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="aria2c download timed out"):
        download_handler._download_url(
            "https://example.com/stuck.bin",
            str(tmp_path),
            filename="stuck.bin",
            timeout_sec=0.2,
        )


# --- progress_callback hook (installer-server SSE bridge) ---
#
# The installer pod's aiohttp server (bead 5f2) needs structured events on a
# callback instead of (or in addition to) runpod's harness progress_update.

def test_progress_callback_receives_download_start_and_done(fake_aria2c, models_base):
    events = []
    download_handler.handle(
        _job([
            {"source": "url", "url": "https://example.com/m.safetensors",
             "dest": "loras", "sha256": REAL_SHA},
        ]),
        progress_callback=events.append,
    )
    types = [e["type"] for e in events]
    assert "download_start" in types, f"expected download_start; got {types}"
    assert "download_done" in types, f"expected download_done; got {types}"
    start = next(e for e in events if e["type"] == "download_start")
    assert start["file_index"] == 0
    assert start["file"] == "m.safetensors"
    done = next(e for e in events if e["type"] == "download_done")
    assert done["file_index"] == 0
    assert done["cached"] is False
    assert done["sha256"] == REAL_SHA


def test_progress_callback_reports_cached_on_dedup_hit(fake_aria2c, models_base):
    dest = models_base / "loras"
    dest.mkdir()
    (dest / "m.safetensors").write_bytes(REAL_BYTES)

    events = []
    download_handler.handle(
        _job([
            {"source": "url", "url": "https://example.com/m.safetensors",
             "dest": "loras", "sha256": REAL_SHA},
        ]),
        progress_callback=events.append,
    )
    done = next(e for e in events if e["type"] == "download_done")
    assert done["cached"] is True
    assert done["sha256"] == REAL_SHA
    assert fake_aria2c["calls"] == 0


def test_progress_callback_omitted_keeps_legacy_behavior(fake_aria2c, models_base):
    seen = []

    def fake_progress(job, payload):
        seen.append(payload)

    original = download_handler.runpod.serverless.progress_update
    download_handler.runpod.serverless.progress_update = fake_progress
    try:
        result = download_handler.handle(_job([
            {"source": "url", "url": "https://example.com/m.safetensors",
             "dest": "loras"},
        ]))
    finally:
        download_handler.runpod.serverless.progress_update = original

    assert result["ok"] is True
    assert seen, "legacy progress_update path must still fire when no callback supplied"



# --- parallelism (download manager) ---

def test_downloads_run_in_parallel(models_base, mocker):
    """Two downloads launched simultaneously must overlap, not serialize.

    Each fake aria2c records its start time, sleeps 200ms (simulating I/O),
    writes the file. With max_workers=2 the total elapsed should be roughly
    one sleep, not two."""
    import time, threading

    started_times: list[float] = []
    started_lock = threading.Lock()

    class _SlowProc:
        def __init__(self, argv, **_):
            with started_lock:
                started_times.append(time.monotonic())
            dest_dir = argv[argv.index("-d") + 1]
            filename = argv[argv.index("-o") + 1]
            os.makedirs(dest_dir, exist_ok=True)
            time.sleep(0.2)
            with open(os.path.join(dest_dir, filename), "wb") as f:
                f.write(REAL_BYTES)
            self.stdout = iter([])
            self.returncode = 0
        def wait(self, timeout=None):
            return 0

    mocker.patch.object(download_handler.subprocess, "Popen", _SlowProc)

    t0 = time.monotonic()
    result = download_handler.handle(_job([
        {"source": "url", "url": "https://example.com/a.safetensors", "dest": "loras"},
        {"source": "url", "url": "https://example.com/b.safetensors", "dest": "loras"},
    ]))
    elapsed = time.monotonic() - t0
    assert result["ok"] is True

    assert len(started_times) == 2
    gap = abs(started_times[1] - started_times[0])
    assert gap < 0.1, f"downloads serialized: start gap = {gap:.3f}s"
    assert elapsed < 0.35, f"total elapsed {elapsed:.3f}s suggests serialization"


def test_parallel_results_preserve_input_order(fake_aria2c, models_base):
    """Even with out-of-order completion, results[i] must correspond to
    downloads[i] — callers index into both lists by position."""
    result = download_handler.handle(_job([
        {"source": "url", "url": "https://example.com/first.safetensors", "dest": "loras"},
        {"source": "url", "url": "https://example.com/second.safetensors", "dest": "loras"},
        {"source": "url", "url": "https://example.com/third.safetensors", "dest": "loras"},
    ]))
    names = [f["filename"] for f in result["files"]]
    assert names == ["first.safetensors", "second.safetensors", "third.safetensors"]


def test_one_failure_in_parallel_batch_fails_the_job(models_base, mocker):
    """If any download raises, the whole batch fails — but already-running
    downloads are allowed to complete (we don't waste partial bandwidth)."""
    import hashlib

    bogus_sha = "0" * 64
    real_sha = hashlib.sha256(REAL_BYTES).hexdigest()

    class _Proc:
        def __init__(self, argv, **_):
            dest_dir = argv[argv.index("-d") + 1]
            filename = argv[argv.index("-o") + 1]
            os.makedirs(dest_dir, exist_ok=True)
            with open(os.path.join(dest_dir, filename), "wb") as f:
                f.write(REAL_BYTES)
            checksum_arg = next((a for a in argv if a.startswith("--checksum=")), None)
            self.returncode = 0
            if checksum_arg:
                expected = checksum_arg.split("=", 2)[2].lower()
                if expected != real_sha:
                    self.returncode = 32
            self.stdout = iter([])
        def wait(self, timeout=None):
            return self.returncode

    mocker.patch.object(download_handler.subprocess, "Popen", _Proc)

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        download_handler.handle(_job([
            {"source": "url", "url": "https://example.com/a.safetensors",
             "dest": "loras", "sha256": real_sha},
            {"source": "url", "url": "https://example.com/b.safetensors",
             "dest": "loras", "sha256": bogus_sha},
        ]))
