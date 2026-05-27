"""Tests for RunPod result/progress race handling in worker command dispatch."""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def worker(monkeypatch):
    import runpod.serverless

    monkeypatch.setattr(runpod.serverless, "start", lambda *a, **k: None)
    sys.modules.pop("worker", None)
    import worker
    return worker


def test_download_command_waits_before_returning(worker, monkeypatch):
    sleeps = []
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: sleeps.append(seconds))

    fake_download = types.SimpleNamespace(
        handle=lambda job: {"ok": True, "job_id": job["id"]},
    )
    monkeypatch.setitem(sys.modules, "download_handler", fake_download)

    result = worker.handler({"id": "job-1", "input": {"command": "download"}})

    assert result == {"ok": True, "job_id": "job-1"}
    assert sleeps == [1]


def test_download_command_waits_before_reraising(worker, monkeypatch):
    sleeps = []
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: sleeps.append(seconds))

    def boom(_job):
        raise RuntimeError("download failed")

    monkeypatch.setitem(sys.modules, "download_handler", types.SimpleNamespace(handle=boom))

    with pytest.raises(RuntimeError, match="download failed"):
        worker.handler({"id": "job-1", "input": {"command": "download"}})

    assert sleeps == [1]
