#!/usr/bin/env python3
"""Wait for a RunPod endpoint's cached worker pods to all reflect a target image tag.

After a template update on a serverless endpoint, RunPod begins a rolling release:
existing cached pods cycle to the new image. We poll the GraphQL endpoint to read
`myself.endpoints[id=...].pods[].imageName` and exit success when every pod's
imageName ends with the target tag (or there are zero pods, which is also a valid
post-rollout state when workersMin == 0).

Used by CircleCI as the step between `update_endpoint` and `smoke_test`.

Auth: RUNPOD_API_KEY env var (Bearer).

Usage:
    python wait_for_rollout.py --endpoint-id <ep> --image-tag <tag> [--timeout 1800] [--interval 30]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

GRAPHQL_URL = "https://api.runpod.io/graphql"


def query_pods(endpoint_id: str, api_key: str) -> list[dict]:
    """Return list of {id, imageName, desiredStatus} for the endpoint's cached pods."""
    body = json.dumps({
        "query": (
            "query { myself { endpoints { id pods { id imageName desiredStatus } } } }"
        )
    }).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # RunPod's WAF rejects Python-urllib/* default UA with 403.
            "User-Agent": "comfygen-ci/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    eps = payload.get("data", {}).get("myself", {}).get("endpoints", []) or []
    for ep in eps:
        if ep["id"] == endpoint_id:
            return ep.get("pods", []) or []
    raise RuntimeError(f"endpoint {endpoint_id!r} not visible to this API key")


def all_on_tag(pods: list[dict], image_tag: str) -> tuple[bool, dict]:
    """True if every pod's imageName ends with `:image_tag`. Also returns counts."""
    suffix = f":{image_tag}"
    matching = [p for p in pods if (p.get("imageName") or "").endswith(suffix)]
    stale = [p for p in pods if not (p.get("imageName") or "").endswith(suffix)]
    return (len(stale) == 0, {
        "total": len(pods),
        "matching": len(matching),
        "stale": len(stale),
        "stale_examples": [p.get("imageName") for p in stale[:3]],
    })


def wait(endpoint_id: str, image_tag: str, timeout: int, interval: int) -> dict:
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        raise RuntimeError("RUNPOD_API_KEY env var not set")

    started = time.time()
    last_status = None
    while time.time() - started < timeout:
        try:
            pods = query_pods(endpoint_id, api_key)
        except urllib.error.URLError as e:
            print(f"[wait] transient API error: {e}; retrying", file=sys.stderr, flush=True)
            time.sleep(interval)
            continue

        ok, status = all_on_tag(pods, image_tag)
        status["elapsed"] = round(time.time() - started, 1)
        if status != last_status:
            print(f"[wait] {json.dumps(status)}", file=sys.stderr, flush=True)
            last_status = status
        if ok:
            return {"ok": True, "endpoint_id": endpoint_id, "image_tag": image_tag, **status}
        time.sleep(interval)

    return {"ok": False, "endpoint_id": endpoint_id, "image_tag": image_tag,
            "error": f"timed out after {timeout}s waiting for rollout",
            **(last_status or {})}


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--endpoint-id", required=True)
    p.add_argument("--image-tag", required=True, help="e.g. v18")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--interval", type=int, default=30)
    args = p.parse_args()

    result = wait(args.endpoint_id, args.image_tag, args.timeout, args.interval)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
