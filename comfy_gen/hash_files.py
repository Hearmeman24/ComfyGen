"""Hash files already on the RunPod network volume via a serverless job.

Lets a caller ask "what's the sha256 of these files you already have?" so they
can decide whether to skip a download. Returns per-path sha256 + bytes (with
per-path errors for missing/inaccessible files).
"""

import json
import urllib.error
import urllib.request
from typing import Any

from comfy_gen import output, poller


def submit_hash(
    paths: list[str],
    timeout: int = 300,
    poll_interval: int = 3,
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    """Submit a hash job to the serverless endpoint.

    Args:
        paths: Absolute paths on /runpod-volume to hash.
        timeout: Max seconds to wait for completion.
        poll_interval: Seconds between status checks.
        endpoint_id: Override endpoint ID from config.

    Returns:
        Result dict: {"ok": bool, "files": [{"path", "sha256", "bytes"} | {"path", "sha256": null, "error"}]}.
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

    payload = {"input": {"command": "hash", "paths": paths}}

    output.log(f"Hashing {len(paths)} file(s) on network volume...")
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

    result = poller.poll_job(
        job_id=job_id,
        endpoint_id=endpoint_id,
        api_key=api_key,
        timeout=timeout,
        poll_interval=poll_interval,
    )

    files = result.get("files", [])
    ok_count = sum(1 for f in files if f.get("sha256"))
    output.log(f"Hashed: {ok_count}/{len(files)} ({len(files) - ok_count} errors)")
    return result
