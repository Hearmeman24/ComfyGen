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
import hashlib
import json
import subprocess
import sys
import time
import urllib.request

MANIFEST_URL = "https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/main/manifest.json"


def choose_workflow(preset: dict) -> dict:
    """Return the single workflow this smoke run will exercise.

    Supports both schemas:
    - legacy: preset['workflow'] is a {name?, url, sha256, smoke_inputs?} dict
    - current (post-sgs-ui-chf): preset['workflows'] is a list; smoke runs the FIRST entry only
      (cheapest matrix; matches preset.tested_against in practice).
    """
    if preset.get("workflow"):
        return preset["workflow"]
    workflows = preset.get("workflows") or []
    if not workflows:
        raise RuntimeError("preset has neither 'workflow' nor a non-empty 'workflows' list")
    return workflows[0]


def fetch_smoke_inputs(smoke_inputs: list[dict], preset_id: str) -> list[tuple[str, str]]:
    """Download + sha256-verify each fixture; return [(node_id, local_path), ...].

    Used to build `comfy-gen submit --input <node_id>=<local_path>` args. Fixtures
    are workflow-specific test inputs declared by the preset (per sgs-ui-5ir
    schema); BlockFlow's installer ignores them.
    """
    out: list[tuple[str, str]] = []
    for i, inp in enumerate(smoke_inputs):
        node_id = inp["node_id"]
        url = inp["url"]
        expected_sha = inp["sha256"]
        filename = inp["filename"]
        local_path = f"/tmp/smoke-{preset_id}-fixture-{i}-{filename}"
        print(f"[smoke] fetching fixture for node {node_id}: {filename}", file=sys.stderr)
        data = urllib.request.urlopen(url, timeout=60).read()
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha:
            raise RuntimeError(
                f"smoke_inputs[{i}] sha256 mismatch for {filename}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        with open(local_path, "wb") as f:
            f.write(data)
        out.append((node_id, local_path))
    return out


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
        # comfy-gen writes error JSON to stdout (output.error()) and progress
        # to stderr (output.log()). Show both so the actual failure surfaces.
        stdout_tail = (proc.stdout or "")[-1500:]
        stderr_tail = (proc.stderr or "")[-500:]
        raise RuntimeError(
            f"{step} failed (exit {proc.returncode}):\n"
            f"--- stdout tail ---\n{stdout_tail}\n"
            f"--- stderr tail ---\n{stderr_tail}"
        )
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

    # Pick the workflow + fetch its JSON. Multi-workflow presets (wan-animate)
    # smoke only the first listed workflow (locked design from kv9; matches
    # 'tested_against' note in practice).
    workflow_entry = choose_workflow(preset)
    wf_name = workflow_entry.get("name", "<unnamed>")
    print(f"[smoke] workflow: {wf_name!r}", file=sys.stderr)
    wf_url = workflow_entry["url"]
    wf_file = f"/tmp/smoke-{preset_id}-workflow.json"
    with open(wf_file, "wb") as f:
        f.write(urllib.request.urlopen(wf_url, timeout=30).read())

    # Pre-flight: validate the workflow's class_types + inputs against the
    # endpoint's installed node set BEFORE submitting. Fails fast with every
    # problem named in one error instead of waiting ~25 min for the worker
    # to hit ComfyUI's own validation and return one cryptic message at a time.
    with open(wf_file) as f:
        workflow = json.load(f)
    class_types = sorted({n.get("class_type") for n in workflow.values() if isinstance(n, dict) and n.get("class_type")})
    info_result = run_cli(
        ["comfy-gen", "object-info", *class_types,
         "--endpoint-id", endpoint_id, "--timeout", "120"],
        step="object_info",
    )
    if not info_result.get("ok"):
        raise RuntimeError(f"object_info call failed: {info_result.get('error', info_result)}")
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
    from validate_workflow import validate  # noqa: E402  -- sibling-dir import at smoke time
    # Fields populated by smoke_inputs are runtime-loaded into ComfyUI/input/
    # via `comfy-gen submit --input` AFTER object_info is queried, so their
    # enum check would false-positive on a stale dropdown. Skip them.
    smoke_inputs_for_skip = workflow_entry.get("smoke_inputs") or []
    skip_enum = {(i["node_id"], i["field"]) for i in smoke_inputs_for_skip}
    failures = validate(workflow, info_result.get("classes", {}), skip_enum_fields=skip_enum)
    if failures:
        joined = "\n  - " + "\n  - ".join(failures)
        raise RuntimeError(f"workflow pre-flight failed ({len(failures)} issue(s)):{joined}")
    print(f"[smoke] pre-flight validated {len(class_types)} class(es), {len(workflow)} node(s)",
          file=sys.stderr)

    # Fetch + sha256-verify any per-workflow smoke_input fixtures (image, video,
    # etc.) and translate them into `--input <node_id>=<path>` args. Absent
    # smoke_inputs is fine for zero-file workflows (qwen-image-lighting).
    input_args: list[str] = []
    smoke_inputs = workflow_entry.get("smoke_inputs") or []
    if smoke_inputs:
        fixtures = fetch_smoke_inputs(smoke_inputs, preset_id)
        for node_id, local_path in fixtures:
            input_args.extend(["--input", f"{node_id}={local_path}"])
        print(f"[smoke] {len(fixtures)} smoke_input fixture(s) fetched", file=sys.stderr)

    # Run the workflow.
    submit_result = run_cli(
        ["comfy-gen", "submit", wf_file, *input_args,
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
