"""RunPod API client for managing serverless infrastructure.

Handles endpoint creation, network volume management, and API key validation
via RunPod's GraphQL and REST APIs.
"""

import json
import urllib.error
import urllib.request
from typing import Any

GRAPHQL_URL = "https://api.runpod.io/graphql"
REST_BASE = "https://rest.runpod.io/v1"

BASE_TEMPLATE_ID = "bdy0gkebsg"
BASE_DOCKER_IMAGE = "hearmeman/comfyui-serverless:v17"
RUNTIME_REPO_URL = "https://github.com/Hearmeman24/remote-comfy-gen-handler.git"
# Docker image is CUDA 12.8.1 — only accept 12.8+
ALLOWED_CUDA_VERSIONS = ["12.9", "12.8"]


def _graphql(api_key: str, query: str) -> dict[str, Any]:
    """Execute a GraphQL query against the RunPod API."""
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps({"query": query}).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "comfy-gen/0.2",
        },
    )
    try:
        resp = json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"RunPod GraphQL API returned {e.code}: {body}")
    if "errors" in resp and not resp.get("data"):
        raise RuntimeError(f"RunPod GraphQL error: {resp['errors'][0].get('message', 'Unknown error')}")
    return resp.get("data", {})


def _rest(api_key: str, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    """Make a REST API call to RunPod."""
    url = f"{REST_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "comfy-gen/0.2",
        },
        method=method,
    )
    try:
        resp_raw = urllib.request.urlopen(req).read()
        if not resp_raw:
            return {}
        return json.loads(resp_raw)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")[:500]
        try:
            err = json.loads(body_text)
            msg = err.get("error", body_text)
        except (json.JSONDecodeError, ValueError):
            msg = body_text
        raise RuntimeError(f"RunPod API error ({e.code}): {msg}")


def validate_api_key(api_key: str) -> bool:
    """Test if a RunPod API key is valid by making a simple query."""
    try:
        data = _graphql(api_key, "{ gpuTypes { id } }")
        return "gpuTypes" in data
    except Exception:
        return False


def list_gpu_types(api_key: str) -> list[dict[str, Any]]:
    """List all available GPU types with pricing."""
    data = _graphql(api_key, "{ gpuTypes { id displayName memoryInGb securePrice communityPrice } }")
    return data.get("gpuTypes", [])


def create_network_volume(api_key: str, name: str, size_gb: int, datacenter_id: str) -> dict[str, Any]:
    """Create a network volume in a specific datacenter.

    Args:
        api_key: RunPod API key.
        name: Volume name.
        size_gb: Size in GB (minimum 10).
        datacenter_id: Datacenter ID (e.g. "EU-RO-1").

    Returns:
        Dict with id, name, size, dataCenterId.
    """
    return _rest(api_key, "POST", "/networkvolumes", {
        "name": name,
        "size": size_gb,
        "dataCenterId": datacenter_id,
    })


def create_template(
    api_key: str,
    name: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a serverless template with the user's environment variables.

    Clones the base ComfyGen Docker image and runtime config, injecting
    the user's S3 credentials and other env vars so workers have them
    at startup without needing them in every job payload.

    Args:
        api_key: RunPod API key.
        name: Template display name.
        env: Environment variables for the worker container.

    Returns:
        Dict with id, name, imageName, env, etc.
    """
    # REST API has a bug: it applies default volumeInGb=20 to serverless
    # templates then rejects it. Use GraphQL instead.
    env_gql = ""
    if env:
        entries = ", ".join(
            f'{{ key: "{k}", value: "{v}" }}' for k, v in env.items()
        )
        env_gql = f"env: [{entries}]"

    query = f"""
    mutation {{
      saveTemplate(input: {{
        name: "{name}"
        imageName: "{BASE_DOCKER_IMAGE}"
        isServerless: true
        containerDiskInGb: 5
        volumeInGb: 0
        dockerArgs: ""
        {env_gql}
      }}) {{
        id
        name
        imageName
        isServerless
      }}
    }}
    """
    data = _graphql(api_key, query)
    return data.get("saveTemplate", {})


def create_endpoint(
    api_key: str,
    name: str,
    template_id: str,
    gpu_type_ids: list[str],
    volume_id: str,
    workers_max: int = 3,
    idle_timeout: int = 5,
    execution_timeout_ms: int = 600000,
) -> dict[str, Any]:
    """Create a serverless endpoint from a template.

    Args:
        api_key: RunPod API key.
        name: Endpoint display name.
        template_id: Template ID to use.
        gpu_type_ids: GPU types in priority order.
        volume_id: Network volume ID to attach.
        workers_max: Maximum concurrent workers.
        idle_timeout: Seconds idle before scale-down.
        execution_timeout_ms: Max milliseconds per job.

    Returns:
        Dict with id, name, gpuTypeIds, networkVolumeId, etc.
    """
    return _rest(api_key, "POST", "/endpoints", {
        "name": name,
        "templateId": template_id,
        "gpuTypeIds": gpu_type_ids,
        "allowedCudaVersions": ALLOWED_CUDA_VERSIONS,
        "workersMin": 0,
        "workersMax": workers_max,
        "idleTimeout": idle_timeout,
        "flashboot": True,
        "networkVolumeId": volume_id,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 4,
        "executionTimeoutMs": execution_timeout_ms,
    })


def get_endpoint(api_key: str, endpoint_id: str) -> dict[str, Any]:
    """Get endpoint details by ID.

    Args:
        api_key: RunPod API key.
        endpoint_id: Endpoint ID to query.

    Returns:
        Dict with endpoint details (id, name, gpuTypeIds, etc.).
    """
    return _rest(api_key, "GET", f"/endpoints/{endpoint_id}")


def get_endpoint_health(api_key: str, endpoint_id: str) -> dict[str, Any]:
    """Get endpoint health status including worker states.

    Uses the serverless /health endpoint which reports worker counts
    by state: initializing, ready, idle, running, throttled, unhealthy.

    Args:
        api_key: RunPod API key.
        endpoint_id: Endpoint ID to query.

    Returns:
        Dict with 'workers' and 'jobs' status counts.
    """
    url = f"https://api.runpod.ai/v2/{endpoint_id}/health"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "comfy-gen/0.2",
        },
    )
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"RunPod health API returned {e.code}: {body}")
