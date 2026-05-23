"""Query ComfyUI's /object_info via the worker's object_info command.

Lets callers introspect what node classes are installed and their accepted
INPUT_TYPES — useful for pre-validating workflows before submission and for
ad-hoc debugging of 'Value not in list' or 'Required input is missing'
errors.
"""

import json
import urllib.error
import urllib.request
from typing import Any

from comfy_gen import output, poller


def submit_object_info(
    class_types: list[str] | None = None,
    timeout: int = 120,
    poll_interval: int = 3,
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    """Submit an object_info job to the serverless endpoint.

    Args:
        class_types: Optional list of class names to filter the response.
            Omit (or pass None) to get every installed class.
        timeout: Max seconds to wait for completion.
        poll_interval: Seconds between status checks.
        endpoint_id: Override endpoint ID from config.

    Returns:
        Result dict: {"ok": bool, "classes": {"<ClassName>": {input, output, ...}}}.
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

    job_input: dict[str, Any] = {"command": "object_info"}
    if class_types:
        job_input["class_types"] = list(class_types)
    payload = {"input": job_input}

    n = "all" if not class_types else f"{len(class_types)}"
    output.log(f"Fetching object_info for {n} class(es)...")
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

    classes = result.get("classes", {})
    output.log(f"Got info for {len(classes)} class(es)")
    return result
