#!/usr/bin/env python3
"""Live smoke test for a single BlockFlow preset against a ComfyGen endpoint.

Fetches the preset manifest, installs the preset's models on the network volume
via `comfy-gen download`, runs the preset's workflow via `comfy-gen submit`, and
verifies each output URL with a Range GET. Exits 0 on success, non-zero on any
step failure. JSON status to stdout, human progress to stderr.

Used by CircleCI as the post-build gate: one job per preset (matrix), each
proves the freshly-deployed image actually serves that preset end-to-end.

Usage:
    python smoke_preset.py <preset_id> --endpoint-id <ep> [--workflow-timeout 1800]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request

MANIFEST_URL = "https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/main/manifest.json"


def fetch_preset(preset_id: str) -> dict:
    """Fetch the preset.json for `preset_id` from the registry."""
    manifest = json.loads(urllib.request.urlopen(MANIFEST_URL, timeout=30).read())
    for p in manifest.get("presets", []):
        if p["id"] == preset_id:
            return json.loads(urllib.request.urlopen(p["preset_url"], timeout=30).read())
    raise RuntimeError(f"preset {preset_id!r} not in registry manifest")


def build_downloads(preset: dict) -> list[dict]:
    """Translate preset.models into the comfy-gen download batch shape.

    Preset model entries use `source: "huggingface"` (a label); the download
    handler only knows `source: "url"` and `source: "civitai"`. HF URLs are
    plain HTTPS so they go through the url path.
    """
    items: list[dict] = []
    for m in preset["models"]:
        items.append({
            "source": "url",
            "url": m["url"],
            "destination_path": m["dest"],
            "sha256": m["sha256"],
        })
    return items


def run_cli(cmd: list[str], step: str) -> dict:
    """Run a comfy-gen subcommand; return parsed JSON stdout. Fail loud on error."""
    print(f"[smoke] {step}: {' '.join(cmd)}", file=sys.stderr, flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(f"{step} failed (exit {proc.returncode}):\n{tail}")
    if not proc.stdout.strip():
        raise RuntimeError(f"{step} produced no stdout. stderr tail:\n{(proc.stderr or '')[-1000:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{step} stdout not JSON: {e}\nstdout[:500]={proc.stdout[:500]!r}")


def verify_output_url(url: str) -> dict:
    """GET with Range to confirm the output is fetchable.

    HEAD doesn't work for S3 presigned-GET URLs (returns 403), but a Range
    request for the first KB does. Returns {status, bytes_returned}.
    """
    req = urllib.request.Request(url, headers={"Range": "bytes=0-1023"})
    with urllib.request.urlopen(req, timeout=60) as r:
        if r.status not in (200, 206):
            raise RuntimeError(f"output URL returned HTTP {r.status}: {url}")
        body = r.read()
        return {"status": r.status, "bytes_returned": len(body)}


def smoke(preset_id: str, endpoint_id: str, workflow_timeout: int) -> dict:
    started = time.time()

    preset = fetch_preset(preset_id)
    print(f"[smoke] preset {preset_id!r} loaded: {len(preset['models'])} model(s)", file=sys.stderr)

    # Install models — dedup will skip anything already on the volume with the matching sha256.
    dl_batch = build_downloads(preset)
    dl_file = f"/tmp/smoke-{preset_id}-downloads.json"
    with open(dl_file, "w") as f:
        json.dump(dl_batch, f)
    dl_result = run_cli(
        ["comfy-gen", "download", "--batch", dl_file,
         "--endpoint-id", endpoint_id, "--timeout", "5400"],
        step="download",
    )
    files = dl_result.get("files", [])
    cached = sum(1 for f in files if f.get("cached"))
    print(f"[smoke] download done: {len(files)} file(s), {cached} cached, "
          f"{len(files) - cached} fresh", file=sys.stderr)

    # Fetch the workflow JSON to a local file (submit takes a path).
    wf_url = preset["workflow"]["url"]
    wf_file = f"/tmp/smoke-{preset_id}-workflow.json"
    with open(wf_file, "wb") as f:
        f.write(urllib.request.urlopen(wf_url, timeout=30).read())

    # Run the workflow.
    submit_result = run_cli(
        ["comfy-gen", "submit", wf_file,
         "--endpoint-id", endpoint_id, "--timeout", str(workflow_timeout)],
        step="submit",
    )
    # Worker returns a single primary output: {"ok": true, "output": {"url": ..., ...}}.
    if not submit_result.get("ok"):
        raise RuntimeError(f"submit returned ok=false: {submit_result}")
    out = submit_result.get("output") or {}
    url = out.get("url")
    if not url:
        raise RuntimeError(f"submit output missing url. result keys: {list(submit_result)}, output keys: {list(out)}")
    v = verify_output_url(url)
    verified = [{"url": url, "resolution": out.get("resolution"), "seed": out.get("seed"), **v}]
    print(f"[smoke] verified output URL", file=sys.stderr)

    return {
        "ok": True,
        "preset_id": preset_id,
        "endpoint_id": endpoint_id,
        "downloads": {"total": len(files), "cached": cached, "fresh": len(files) - cached},
        "outputs": verified,
        "workflow_elapsed_seconds": submit_result.get("elapsed_seconds"),
        "smoke_elapsed_seconds": round(time.time() - started, 1),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("preset_id", help="Preset ID from the blockflow-presets manifest")
    p.add_argument("--endpoint-id", required=True, help="RunPod endpoint to test against")
    p.add_argument("--workflow-timeout", type=int, default=1800,
                   help="Max seconds for the workflow run (default 1800)")
    args = p.parse_args()

    try:
        result = smoke(args.preset_id, args.endpoint_id, args.workflow_timeout)
        print(json.dumps(result))
        sys.exit(0)
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "preset_id": args.preset_id,
            "endpoint_id": args.endpoint_id,
            "error": str(e),
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
