"""Download models to the RunPod network volume via serverless jobs."""

import json
import os
import urllib.error
import urllib.request
from typing import Any

from comfy_gen import output, poller


def submit_download(
    downloads: list[dict[str, Any]],
    timeout: int = 600,
    poll_interval: int = 5,
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    """Submit a download job to the serverless endpoint.

    Args:
        downloads: List of download specs, each with source/dest/etc.
        timeout: Max seconds to wait for completion.
        poll_interval: Seconds between status checks.

    Returns:
        Result dict from the worker.
    """
    from comfy_gen import config

    cfg = config.load()
    api_key = cfg.get("runpod_api_key", "")
    if not endpoint_id:
        endpoint_id = cfg.get("endpoint_id", "")

    if not api_key:
        raise ValueError(
            "No RunPod API key configured. Run 'comfy-gen init' or set via:\n"
            "  comfy-gen config --set runpod_api_key=rpa_..."
        )
    if not endpoint_id:
        raise ValueError(
            "No RunPod endpoint configured. Run 'comfy-gen init' or set via:\n"
            "  comfy-gen config --set endpoint_id=<id>"
        )

    # Check for CivitAI token if any downloads use civitai source
    has_civitai = any(d.get("source") == "civitai" for d in downloads)
    civitai_token = cfg.get("civitai_token", "") or os.environ.get("CIVITAI_TOKEN", "")
    if has_civitai and not civitai_token:
        raise ValueError(
            "CivitAI downloads require an API token. Set via:\n"
            "  comfy-gen config --set civitai_token=<your-token>\n"
            "  or env var CIVITAI_TOKEN\n"
            "Get your token at: https://civitai.com/user/account"
        )

    payload: dict = {
        "input": {
            "command": "download",
            "downloads": downloads,
        }
    }
    if civitai_token:
        payload["input"]["civitai_token"] = civitai_token
    # Pass the orchestrator-side timeout to the worker so its per-subprocess
    # timeouts (aria2c, civitai-downloader) scale with the polling timeout.
    # Without this, BlockFlow's --timeout flag is cosmetic — the worker's
    # hardcoded subprocess cap would kill long downloads before the
    # orchestrator's polling loop knows anything happened.
    payload["input"]["timeout_sec"] = int(timeout)

    # Submit to RunPod
    output.log(f"Submitting download job ({len(downloads)} file(s))...")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.runpod.ai/v2/{endpoint_id}/run",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"RunPod API returned {e.code}: {body}")

    job_id = resp.get("id")
    if not job_id:
        raise RuntimeError(f"RunPod API did not return a job ID: {resp}")

    output.log(f"Job submitted: {job_id}")

    from comfy_gen import _progress_format

    def _progress(elapsed, status, prog):
        msg = prog.get("message", "")
        pct = prog.get("percent")
        stage = prog.get("stage", "download")
        # Reformat into the canonical progress-line shape when possible so
        # BlockFlow's progress parser sees a stable structure (bead bmq.5 /
        # A.1.2). Non-N/M lines (status, generic messages) stay as-is.
        canonical = _progress_format.try_format_from_message(elapsed, stage, msg, pct)
        if canonical:
            output.log(canonical)
        elif msg and pct is not None:
            output.log(f"[{elapsed}s] {msg} ({pct:.0f}%)")
        elif msg:
            output.log(f"[{elapsed}s] {msg}")
        else:
            output.log(f"[{elapsed}s] {status}")

    result = poller.poll_job(
        job_id=job_id,
        endpoint_id=endpoint_id,
        api_key=api_key,
        timeout=timeout,
        poll_interval=poll_interval,
        progress_fn=_progress,
    )

    files = result.get("files", [])
    exec_time = result.get("elapsed_seconds", 0)
    output.log(f"Download complete: {len(files)} file(s) in {exec_time}s")
    for f in files:
        output.log(f"  {f.get('filename', '?')} ({f.get('size_mb', '?')} MB) -> {f.get('dest', '?')}")
    return result
