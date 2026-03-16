"""Serverless workflow execution via RunPod API + S3."""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from comfy_gen import output


def _runpod_api_key() -> str:
    """Get RunPod API key from config or env."""
    from comfy_gen import config
    cfg = config.load()
    key = cfg.get("runpod_api_key", "") or os.environ.get("RUNPOD_API_KEY", "")
    if not key:
        raise ValueError(
            "No RunPod API key configured. Set via:\n"
            "  comfy-gen config --set runpod_api_key=rpa_...\n"
            "  or env var RUNPOD_API_KEY"
        )
    return key


def _endpoint_id(override: str | None = None) -> str:
    """Get RunPod endpoint ID from override, config, or env."""
    if override:
        return override
    from comfy_gen import config
    cfg = config.load()
    eid = cfg.get("endpoint_id", "") or os.environ.get("RUNPOD_ENDPOINT_ID", "")
    if not eid:
        raise ValueError(
            "No RunPod endpoint ID configured. Set via:\n"
            "  comfy-gen config --set endpoint_id=<id>\n"
            "  or env var RUNPOD_ENDPOINT_ID"
        )
    return eid


def _upload_input(local_path: str, cfg: dict | None = None) -> str:
    """Upload a local file and return a URL the worker can download from."""
    from comfy_gen import storage
    return storage.upload_input(local_path, config=cfg)


def _detect_file_inputs(workflow: dict) -> dict[str, dict]:
    """Find LoadImage nodes in a workflow that reference local files.

    Returns {node_id: {"field": "image", "local_path": "...", "filename": "..."}}
    for nodes whose image value looks like a local file path.
    """
    file_inputs = {}
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type", "")
        if class_type != "LoadImage":
            continue
        image_val = node.get("inputs", {}).get("image", "")
        if isinstance(image_val, str) and image_val and os.path.isfile(image_val):
            file_inputs[node_id] = {
                "field": "image",
                "local_path": image_val,
                "filename": Path(image_val).name,
            }
    return file_inputs


def _format_comfy_errors(comfy_err: dict) -> str:
    """Format a parsed ComfyUI error JSON into a readable message."""
    err_type = comfy_err.get("error", {}).get("type", "")
    err_msg = comfy_err.get("error", {}).get("message", "")
    extra = comfy_err.get("error", {}).get("extra_info", {})
    node_errors = comfy_err.get("node_errors", {})

    # Missing custom node
    if err_type == "missing_node_type":
        node_title = extra.get("node_title", "")
        class_type = extra.get("class_type", "")
        return f"Missing custom node: {node_title or class_type}"

    # Validation errors with per-node details
    if node_errors:
        lines = ["Workflow validation failed:"]
        for node_id, info in node_errors.items():
            class_type = info.get("class_type", node_id)
            for e in info.get("errors", []):
                details = e.get("details", e.get("message", "unknown error"))
                # The details string can be very long with full model lists.
                # Extract just the key info: what's missing and from which input.
                input_name = e.get("extra_info", {}).get("input_name", "")
                received = e.get("extra_info", {}).get("received_value", "")
                if input_name and received:
                    lines.append(f"  Node {node_id} ({class_type}): '{received}' not found for input '{input_name}'")
                else:
                    # Truncate long details
                    if len(details) > 200:
                        details = details[:200] + "..."
                    lines.append(f"  Node {node_id} ({class_type}): {details}")
        return "\n".join(lines)

    # Generic ComfyUI error
    if err_msg:
        return f"ComfyUI error: {err_msg}"

    return str(comfy_err)


def _format_job_error(raw_error: str) -> str:
    """Extract a human-readable error from the worker's raw error payload.

    Handles multiple formats:
    - New: clean message from _clean_error (already readable)
    - Old: JSON envelope with error_type/error_message/error_traceback
    - Raw: ComfyUI JSON embedded in error string
    """
    if not raw_error:
        return "Unknown error"

    # Try to parse as JSON
    try:
        err = json.loads(raw_error) if isinstance(raw_error, str) else raw_error
    except (json.JSONDecodeError, ValueError):
        err = None

    # Case 1: Worker error envelope {error_type, error_message, error_traceback}
    if isinstance(err, dict) and "error_message" in err:
        msg = err["error_message"]
    elif isinstance(err, dict):
        # Could be a ComfyUI error directly
        if "error" in err and "node_errors" in err:
            return _format_comfy_errors(err)
        msg = str(raw_error)
    else:
        msg = str(raw_error)

    # Try to find embedded ComfyUI JSON in the message
    json_start = msg.find('{"error"')
    if json_start != -1:
        try:
            comfy_err = json.loads(msg[json_start:])
            return _format_comfy_errors(comfy_err)
        except (json.JSONDecodeError, ValueError):
            pass

    # Strip traceback noise — return first meaningful line
    # Remove "Job failed after Ns: " prefix
    if "Job failed after" in msg:
        idx = msg.find(": ")
        if idx != -1:
            msg = msg[idx + 2:]

    # Remove "ComfyUI /prompt returned 400: " prefix if no JSON follows
    if "ComfyUI /prompt returned" in msg:
        idx = msg.find(": ")
        if idx != -1:
            remainder = msg[idx + 2:]
            if not remainder.startswith("{"):
                msg = remainder

    clean = msg.split("\n")[0] if "\n" in msg else msg
    return clean


def submit(
    workflow_path: str,
    file_inputs: dict[str, str] | None = None,
    overrides: dict[str, dict] | None = None,
    timeout: int = 1200,
    poll_interval: int = 3,
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    """Submit a workflow to the serverless endpoint.

    Args:
        workflow_path: Path to ComfyUI API-format workflow JSON.
        file_inputs: Manual file inputs {node_id: local_file_path}.
        overrides: Parameter overrides {node_id: {param: value}}.
        timeout: Max seconds to wait for completion.
        poll_interval: Seconds between status checks.
        endpoint_id: RunPod endpoint ID override (uses config if None).

    Returns:
        Result dict with images, videos, elapsed_seconds, prompt_id.
    """
    api_key = _runpod_api_key()
    endpoint_id = _endpoint_id(endpoint_id)

    # Load workflow
    with open(workflow_path) as f:
        workflow = json.load(f)

    # Validate it's API format
    has_class_type = any(
        isinstance(v, dict) and "class_type" in v
        for v in workflow.values()
    )
    if not has_class_type:
        raise ValueError("Workflow is not in ComfyUI API format (no class_type found). Export via 'Save (API Format)'.")

    # Build file_inputs payload
    payload_file_inputs = {}

    # Auto-detect LoadImage nodes with local file paths
    auto_detected = _detect_file_inputs(workflow)
    for node_id, info in auto_detected.items():
        output.log(f"Uploading input file: {info['local_path']}")
        url = _upload_input(info["local_path"])
        payload_file_inputs[node_id] = {
            "field": info["field"],
            "url": url,
            "filename": info["filename"],
        }

    # Manual file inputs override auto-detected
    if file_inputs:
        for node_id, local_path in file_inputs.items():
            output.log(f"Uploading input file for node {node_id}: {local_path}")
            url = _upload_input(local_path)
            # Detect correct field from node class_type
            node = workflow.get(node_id, {})
            class_type = node.get("class_type", "") if isinstance(node, dict) else ""
            field = "video" if class_type in ("VHS_LoadVideo", "LoadVideo") else "image"
            payload_file_inputs[node_id] = {
                "field": field,
                "url": url,
                "filename": Path(local_path).name,
            }

    # Build payload
    payload = {
        "input": {
            "workflow": workflow,
            "timeout": timeout,
        }
    }
    if payload_file_inputs:
        payload["input"]["file_inputs"] = payload_file_inputs
    if overrides:
        payload["input"]["overrides"] = overrides

    # Log the full request to a file for debugging
    log_path = Path.home() / ".comfy-gen" / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as logf:
        logf.write(f"\n{'='*80}\n")
        logf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] POST https://api.runpod.ai/v2/{endpoint_id}/run\n")
        logf.write(json.dumps(payload, indent=2))
        logf.write("\n")
    output.log(f"Full request logged to {log_path.resolve()}")

    # Submit to RunPod
    output.log("Submitting to RunPod serverless endpoint...")
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

    # Poll for completion
    status_url = f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}"
    elapsed = 0
    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        req = urllib.request.Request(status_url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            resp = json.loads(urllib.request.urlopen(req).read())
        except Exception:
            continue

        status = resp.get("status", "UNKNOWN")

        if status == "COMPLETED":
            worker_output = resp.get("output", {})
            delay = resp.get("delayTime", 0) // 1000
            exec_time = resp.get("executionTime", 0) // 1000

            # Pass through worker output, add job metadata
            worker_output["job_id"] = job_id
            worker_output["delay_seconds"] = delay
            worker_output["elapsed_seconds"] = exec_time

            # Handler returned an error
            if "error_message" in worker_output:
                raise RuntimeError(worker_output["error_message"])
            if "error" in worker_output:
                raise RuntimeError(worker_output["error"])
            if not worker_output.get("ok", True):
                raise RuntimeError(worker_output.get("error_message", "Unknown error"))

            url = worker_output.get("output", {}).get("url", "")
            ext = url.rsplit(".", 1)[-1].lower() if url else ""
            media_type = "video" if ext in ("mp4", "webm", "avi", "mov", "mkv", "gif") else "image"
            output.log(
                f"Completed in {exec_time}s "
                f"(+{delay}s queue). "
                f"1 {media_type}"
            )
            return worker_output

        elif status == "FAILED":
            error_msg = resp.get("error", "Unknown error")
            raise RuntimeError(_format_job_error(error_msg))

        elif status == "TIMED_OUT":
            raise RuntimeError(f"Job timed out on server after {elapsed}s")

        elif status == "CANCELLED":
            raise RuntimeError("Job was cancelled")

        # Show progress details if available
        if status == "IN_PROGRESS":
            prog = resp.get("output", {})
            msg = prog.get("message", "")
            pct = prog.get("percent")
            stage = prog.get("stage", "")
            completed = prog.get("completed_nodes")
            total = prog.get("total_nodes")

            # Build node progress prefix from structured fields
            node_prefix = f"({completed}/{total}) " if completed and total else ""

            if msg and pct is not None:
                output.log(f"[{elapsed}s] {stage}: {node_prefix}{msg} ({pct:.0f}%)")
            elif msg:
                output.log(f"[{elapsed}s] {stage}: {node_prefix}{msg}")
            else:
                output.log(f"[{elapsed}s] {status}")
        else:
            output.log(f"[{elapsed}s] {status}")

    raise TimeoutError(f"Job did not complete within {timeout}s (last status: {status})")


def status(job_id: str, endpoint_id: str | None = None) -> dict[str, Any]:
    """Check the status of a serverless job."""
    api_key = _runpod_api_key()
    endpoint_id = _endpoint_id(endpoint_id)

    req = urllib.request.Request(
        f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )

    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"RunPod API returned {e.code}: {body}")

    runpod_status = resp.get("status", "UNKNOWN")

    result: dict[str, Any] = {
        "job_id": job_id,
        "status": runpod_status.lower(),
    }

    if runpod_status == "COMPLETED":
        worker_output = resp.get("output", {})
        result.update(worker_output)
        result["delay_seconds"] = resp.get("delayTime", 0) // 1000
        result["elapsed_seconds"] = resp.get("executionTime", 0) // 1000
    elif runpod_status == "FAILED":
        result["error"] = _format_job_error(resp.get("error", "Unknown error"))

    return result


def cancel(job_id: str, endpoint_id: str | None = None) -> dict[str, Any]:
    """Cancel a serverless job."""
    api_key = _runpod_api_key()
    endpoint_id = _endpoint_id(endpoint_id)

    req = urllib.request.Request(
        f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{job_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        method="POST",
    )

    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"RunPod API returned {e.code}: {body}")

    return {"job_id": job_id, "status": "cancelled"}
