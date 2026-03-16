"""Shared polling logic for RunPod serverless jobs.

All comfy-gen commands that submit jobs and wait for completion use this
module. Handles the RunPod SDK bug where jobs get stuck at IN_PROGRESS
after the worker reports 100% completion under concurrent load.
"""

import json
import time
import urllib.request
from typing import Any

from comfy_gen import output


# When the worker reports 100% but RunPod doesn't transition to COMPLETED,
# wait this many seconds before treating the job as done.
DONE_GRACE_SECONDS = 30


def poll_job(
    job_id: str,
    endpoint_id: str,
    api_key: str,
    timeout: int = 600,
    poll_interval: int = 5,
    progress_fn=None,
) -> dict[str, Any]:
    """Poll a RunPod job until completion.

    Args:
        job_id: RunPod job ID.
        endpoint_id: RunPod endpoint ID.
        api_key: RunPod API key.
        timeout: Max seconds to wait.
        poll_interval: Seconds between status checks.
        progress_fn: Optional callback(elapsed, status, progress_data) for
                     custom progress display. If None, logs generic progress.

    Returns:
        The job's output dict (from resp["output"]) with job_id added.

    Raises:
        RuntimeError: On job failure, cancellation, or server timeout.
        TimeoutError: If polling exceeds the timeout.
    """
    status_url = f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}"
    elapsed = 0
    status = "UNKNOWN"
    done_since = None

    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        req = urllib.request.Request(
            status_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            resp = json.loads(urllib.request.urlopen(req).read())
        except Exception:
            continue

        status = resp.get("status", "UNKNOWN")

        if status == "COMPLETED":
            worker_output = resp.get("output", {})
            worker_output["job_id"] = job_id
            exec_time = resp.get("executionTime", 0) // 1000
            if exec_time:
                worker_output["elapsed_seconds"] = exec_time
            return worker_output

        elif status == "FAILED":
            error_msg = resp.get("error", "Unknown error")
            raise RuntimeError(error_msg)

        elif status == "TIMED_OUT":
            raise RuntimeError("Job timed out on server")

        elif status == "CANCELLED":
            raise RuntimeError("Job was cancelled")

        # Handle IN_PROGRESS with completion detection
        if status == "IN_PROGRESS":
            prog = resp.get("output", {})
            msg = prog.get("message", "")
            pct = prog.get("percent")

            # RunPod SDK bug: worker finished but status stuck at IN_PROGRESS.
            # Detect via 100% progress and apply grace period.
            if pct is not None and pct >= 100:
                if done_since is None:
                    done_since = elapsed
                elif elapsed - done_since >= DONE_GRACE_SECONDS:
                    output.log("Job complete (worker finished, RunPod status delayed)")
                    return {"ok": True, "message": msg, "job_id": job_id}
            else:
                done_since = None

            # Custom progress display or default
            if progress_fn:
                progress_fn(elapsed, status, prog)
            elif msg and pct is not None:
                output.log(f"[{elapsed}s] {msg} ({pct:.0f}%)")
            elif msg:
                output.log(f"[{elapsed}s] {msg}")
            else:
                output.log(f"[{elapsed}s] {status}")
        else:
            output.log(f"[{elapsed}s] {status}")

    raise TimeoutError(f"Job did not complete within {timeout}s (last status: {status})")
