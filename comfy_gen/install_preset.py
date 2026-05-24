"""ComfyGen `install-preset` / `install-call` — drive the installer pod.

`install-preset`:
  1. Spawn a CPU installer pod (REST /v1/pods) with INSTALLER_TOKEN env.
  2. Poll `https://<pod_id>-<port>.proxy.runpod.net/health` until ready.
  3. POST /install/<preset_id> and stream the SSE response.
  4. Print each event as one JSON line to stdout (BlockFlow pipes this directly).
  5. POST /shutdown unless --keep-alive.
  6. Exit 0 on install_done.ok, 1 on install_error / preflight_fail / timeout.

`install-call`:
  Same as steps 3-5 against an existing pod (BlockFlow's multi-op flow).

All HTTP I/O goes through the module-level functions below so tests can patch
at a stable boundary without standing up real RunPod or aiohttp.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.error
import urllib.request

from comfy_gen import config, output


DEFAULT_PORT = 3000
DEFAULT_IMAGE = "hearmeman/comfyui-serverless:installer-v5"
DEFAULT_HEALTH_TIMEOUT_SEC = 180
HEALTH_POLL_INTERVAL_SEC = 3
SSE_READ_TIMEOUT_SEC = 3600  # multi-GB download — let it run


def _proxy_url(pod_id: str, port: int) -> str:
    return f"https://{pod_id}-{port}.proxy.runpod.net"


def _http(method: str, url: str, *, headers: dict | None = None,
          body: dict | None = None, timeout: float = 30) -> tuple[int, dict | None]:
    """Generic HTTP call returning (status, parsed_json_or_None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = None
        return e.code, payload
    raw = resp.read()
    if not raw:
        return resp.status, None
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, None


def spawn_installer_pod(
    api_key: str,
    image: str,
    volume_id: str,
    token: str,
    name: str = "comfygen-installer",
    port: int = DEFAULT_PORT,
) -> dict:
    """POST /v1/pods to create a CPU pod with installer_server running.

    Returns the parsed JSON from RunPod, which must include `id`. Raises
    RuntimeError on a non-2xx response.
    """
    body = {
        "name": name,
        "imageName": image,
        "containerDiskInGb": 5,
        "volumeMountPath": "/workspace",
        "networkVolumeId": volume_id,
        "ports": f"{port}/http",
        "env": {"INSTALLER_TOKEN": token},
    }
    status, payload = _http(
        "POST", "https://rest.runpod.io/v1/pods",
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
    )
    if status >= 300 or not payload or "id" not in payload:
        raise RuntimeError(f"pod spawn failed ({status}): {payload}")
    return payload


def delete_pod(api_key: str, pod_id: str) -> None:
    """Best-effort DELETE — used when the in-pod /shutdown fallback failed."""
    _http(
        "DELETE", f"https://rest.runpod.io/v1/pods/{pod_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )


def wait_for_health(pod_id: str, port: int, timeout_sec: int) -> None:
    """Poll /health until 200/{ok:true} or timeout. Raises on timeout."""
    deadline = time.monotonic() + timeout_sec
    url = f"{_proxy_url(pod_id, port)}/health"
    last_err: str | None = None
    while time.monotonic() < deadline:
        try:
            status, payload = _http("GET", url, timeout=5)
            if status == 200 and payload and payload.get("ok"):
                return
            last_err = f"status={status} payload={payload}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(HEALTH_POLL_INTERVAL_SEC)
    raise RuntimeError(f"pod {pod_id} not healthy after {timeout_sec}s; last={last_err}")


def stream_install(pod_id: str, port: int, token: str, preset_id: str,
                   civitai_token: str | None = None,
                   hf_token: str | None = None) -> "Iterable[dict]":
    """Yield each parsed SSE event from POST /install/<preset_id>."""
    body: dict = {}
    if civitai_token:
        body["civitai_token"] = civitai_token
    if hf_token:
        body["hf_token"] = hf_token
    req = urllib.request.Request(
        f"{_proxy_url(pod_id, port)}/install/{preset_id}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Installer-Token": token,
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=SSE_READ_TIMEOUT_SEC)
    for raw in resp:
        line = raw.decode().rstrip("\r\n")
        if line.startswith("data: "):
            yield json.loads(line[6:])


def shutdown_pod(pod_id: str, port: int, token: str) -> None:
    """POST /shutdown — pod self-terminates after ~5s. Errors are non-fatal."""
    try:
        _http(
            "POST", f"{_proxy_url(pod_id, port)}/shutdown",
            headers={"X-Installer-Token": token},
            timeout=10,
        )
    except Exception:
        pass


def run(
    preset_id: str,
    *,
    volume_id: str | None,
    pod_id: str | None,
    token: str | None,
    image: str = DEFAULT_IMAGE,
    port: int = DEFAULT_PORT,
    health_timeout_sec: int = DEFAULT_HEALTH_TIMEOUT_SEC,
    keep_alive: bool = False,
    civitai_token: str | None = None,
    hf_token: str | None = None,
    out=sys.stdout,
) -> int:
    """Drive an install end-to-end. Prints one JSON event per line to `out`."""
    spawned_pod = False
    pod_token = token
    cfg = config.load()
    api_key = cfg.get("runpod_api_key", "")

    if pod_id is None:
        if not volume_id:
            raise RuntimeError("--volume-id required when spawning a new pod")
        if not api_key:
            raise RuntimeError("runpod_api_key not configured")
        pod_token = pod_token or secrets.token_urlsafe(24)
        spawn_result = spawn_installer_pod(
            api_key=api_key, image=image, volume_id=volume_id,
            token=pod_token, port=port,
        )
        pod_id = spawn_result["id"]
        spawned_pod = True
        # First stdout line: emit the pod_id immediately so BlockFlow can show
        # the "View live logs ↗" link before the install starts.
        print(json.dumps({"type": "pod_spawned", "pod_id": pod_id, "token": pod_token}),
              file=out, flush=True)

    if pod_token is None:
        raise RuntimeError("--token required when --pod-id is set")

    try:
        wait_for_health(pod_id, port, timeout_sec=health_timeout_sec)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"type": "install_error", "stage": "health",
                          "reason": str(exc)}), file=out, flush=True)
        return 1

    install_ok: bool | None = None
    try:
        for event in stream_install(pod_id, port, pod_token, preset_id,
                                    civitai_token=civitai_token,
                                    hf_token=hf_token):
            print(json.dumps(event), file=out, flush=True)
            if event["type"] == "install_done":
                install_ok = bool(event.get("ok"))
            elif event["type"] in ("install_error", "preflight_fail"):
                install_ok = False
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"type": "install_error", "stage": "stream",
                          "reason": f"{type(exc).__name__}: {exc}"}),
              file=out, flush=True)
        install_ok = False

    if not keep_alive:
        shutdown_pod(pod_id, port, pod_token)
        if spawned_pod and api_key and install_ok is False:
            # Pod stays alive per edge-case table when install fails, so the
            # user can inspect logs. Skip DELETE in that branch.
            pass

    return 0 if install_ok else 1
