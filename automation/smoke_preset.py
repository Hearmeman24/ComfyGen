#!/usr/bin/env python3
"""Live smoke test for a single BlockFlow preset against a ComfyGen endpoint.

Fetches the preset manifest, installs the preset's models on the network volume
via `comfy-gen install-preset` (CPU installer pod, see bead 5f2), runs the
preset's workflow via `comfy-gen submit`, and verifies each output URL with a
Range GET. Exits 0 on success, non-zero on any step failure. JSON status to
stdout, human progress to stderr.

Used by CircleCI as the post-build gate: one job per preset (matrix), each
proves the freshly-deployed image actually serves that preset end-to-end.

Usage:
    python smoke_preset.py <preset_id> --endpoint-id <ep> [--workflow-timeout 1800]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

MANIFEST_URL = "https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/main/manifest.json"

# Substrings that mark a RunPod CPU-pod spawn failure as a transient capacity
# issue (not a hard configuration / auth error). The CLI's spawn loop emits
# "pod spawn failed — no CPU instance available" once every candidate SKU has
# been exhausted; RunPod's GraphQL surfaces "SUPPLY_CONSTRAINT" in the error
# payload for the same condition.
_SUPPLY_CONSTRAINT_MARKERS = ("SUPPLY_CONSTRAINT", "no CPU instance available")


class SupplyConstrainedError(RuntimeError):
    """install-preset gave up because no CPU SKU had capacity. Caller should
    retry with backoff rather than failing the CI run."""


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


def resolve_volume_for_endpoint(api_key: str, endpoint_id: str) -> str:
    """GET /v1/endpoints/<ep> → first network volume id.

    Smoke runs the install onto whatever volume the endpoint already has
    attached — that's the same volume the worker will see at workflow
    time, so files written there are immediately discoverable.
    """
    url = f"https://rest.runpod.io/v1/endpoints/{endpoint_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "comfygen-smoke/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    # The REST schema has used both `networkVolumeIds: [...]` and a singular
    # `networkVolumeId` field across versions. Accept either.
    ids = data.get("networkVolumeIds")
    if isinstance(ids, list) and ids:
        if len(ids) > 1:
            print(f"[smoke] WARN: endpoint {endpoint_id!r} has {len(ids)} "
                  f"volumes; using first ({ids[0]!r})", file=sys.stderr)
        return ids[0]
    singular = data.get("networkVolumeId")
    if singular:
        return singular
    raise RuntimeError(
        f"endpoint {endpoint_id!r} has no network volume attached; "
        f"attach a volume before running smoke"
    )


def run_install_preset(preset_id: str, volume_id: str,
                       runtime_repo_ref: str | None = None,
                       timeout: int = 3600) -> dict:
    """Drive `comfy-gen install-preset` and consume its line-delimited JSON.

    Returns `{total, cached, fresh, files, elapsed_sec, pod_id?}`. Raises
    RuntimeError on `install_error`, `preflight_fail`, `install_done.ok==False`,
    or non-zero exit with no terminal event. `pod_id` is populated from the
    first `pod_spawned` event so failure surfaces are debuggable via the
    RunPod console.
    """
    cmd = [
        "comfy-gen", "install-preset",
        "--preset-id", preset_id,
        "--volume-id", volume_id,
    ]
    if runtime_repo_ref:
        cmd += ["--runtime-repo-ref", runtime_repo_ref]
    print(f"[smoke] install-preset: {' '.join(cmd)}", file=sys.stderr, flush=True)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    files: list[dict] = []
    cached = 0
    fresh = 0
    elapsed_sec: int | None = None
    pod_id: str | None = None
    terminal: dict | None = None
    error: str | None = None
    # When the CLI hits an early RuntimeError (no API key, GraphQL refusal,
    # SUPPLY_CONSTRAINT, etc.) it emits `{"status":"error","error":"..."}` to
    # stdout via output.error() and exits 1. Capture the LAST such line so we
    # can surface it instead of reporting "stderr tail: (empty)".
    cli_error: str | None = None

    assert proc.stdout is not None
    for raw in proc.stdout:
        try:
            event = json.loads(raw.decode().rstrip("\r\n"))
        except json.JSONDecodeError:
            continue
        et = event.get("type")
        if et == "pod_spawned":
            pod_id = event.get("pod_id")
        elif et == "download_done":
            if event.get("cached"):
                cached += 1
            else:
                fresh += 1
        elif et == "install_done":
            terminal = event
            files = event.get("files", [])
            elapsed_sec = event.get("elapsed_sec")
            if not event.get("ok"):
                error = f"install_done.ok=False (pod {pod_id})"
            break
        elif et in ("install_error", "preflight_fail"):
            terminal = event
            error = (
                f"{et} (pod {pod_id}, stage={event.get('stage','?')}): "
                f"{event.get('reason', '<no reason>')}"
            )
            break
        elif event.get("status") == "error":
            cli_error = event.get("error") or json.dumps(event)

    proc.wait(timeout=timeout)
    if error:
        raise RuntimeError(error)
    if terminal is None:
        if cli_error is not None:
            exc = SupplyConstrainedError(cli_error) \
                if any(m in cli_error for m in _SUPPLY_CONSTRAINT_MARKERS) \
                else RuntimeError(cli_error)
            raise exc
        stderr_tail = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[-500:]
        raise RuntimeError(
            f"install-preset unexpected exit ({proc.returncode}) with no "
            f"terminal event. stderr tail:\n{stderr_tail}"
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"install-preset exit {proc.returncode} after install_done — "
            f"likely shutdown failure. files={len(files)} pod={pod_id}"
        )
    return {
        "total": len(files),
        "cached": cached,
        "fresh": fresh,
        "files": files,
        "elapsed_sec": elapsed_sec,
        "pod_id": pod_id,
    }


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


def smoke(preset_id: str, endpoint_id: str, workflow_timeout: int,
          runtime_repo_ref: str | None = None) -> dict:
    started = time.time()

    preset = fetch_preset(preset_id)
    print(f"[smoke] preset {preset_id!r} loaded: {len(preset['models'])} model(s)", file=sys.stderr)

    # Install models on a CPU installer pod (bead 5f2) — dedup will skip
    # anything already on the volume with the matching sha256. The CLI
    # resolves the preset itself; no batch-file translation here.
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        raise RuntimeError("RUNPOD_API_KEY env required to resolve endpoint volume")
    volume_id = resolve_volume_for_endpoint(api_key, endpoint_id)
    print(f"[smoke] installing onto volume {volume_id!r}", file=sys.stderr)
    install_result = run_install_preset(
        preset_id=preset_id,
        volume_id=volume_id,
        runtime_repo_ref=runtime_repo_ref,
    )
    files = install_result["files"]
    cached = install_result["cached"]
    fresh = install_result["fresh"]
    print(f"[smoke] install done: {len(files)} file(s), {cached} cached, "
          f"{fresh} fresh, pod={install_result.get('pod_id')}, "
          f"elapsed={install_result.get('elapsed_sec')}s", file=sys.stderr)

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
        "volume_id": volume_id,
        "installer_pod_id": install_result.get("pod_id"),
        "downloads": {"total": len(files), "cached": cached, "fresh": fresh},
        "install_elapsed_seconds": install_result.get("elapsed_sec"),
        "outputs": verified,
        "workflow_elapsed_seconds": submit_result.get("elapsed_seconds"),
        "smoke_elapsed_seconds": round(time.time() - started, 1),
    }


def run_with_retry(attempt, max_wall_seconds: int = 300,
                   initial_backoff: float = 30.0,
                   max_backoff: float = 120.0) -> dict:
    """Run `attempt()` and retry on SupplyConstrainedError with exponential
    backoff (30s → 60s → 120s, capped). Give up after `max_wall_seconds` of
    wall time and return a skip envelope so the CI job can exit 0.

    Non-supply exceptions propagate. The skip envelope is shaped so the
    smoke main loop's `print(json.dumps(result))` still produces valid
    smoke output.
    """
    deadline = time.monotonic() + max_wall_seconds
    delay = initial_backoff
    last_msg: str | None = None
    while True:
        try:
            return attempt()
        except SupplyConstrainedError as exc:
            last_msg = str(exc)
            now = time.monotonic()
            if now >= deadline:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": f"CPU supply constraint persisted for "
                              f"{max_wall_seconds}s; last={last_msg}",
                }
            remaining = deadline - now
            sleep_for = min(delay, remaining)
            print(f"[smoke] SUPPLY_CONSTRAINT — retrying in {sleep_for:.0f}s "
                  f"(budget {remaining:.0f}s left). last={last_msg}",
                  file=sys.stderr, flush=True)
            time.sleep(sleep_for)
            delay = min(delay * 2, max_backoff)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("preset_id", help="Preset ID from the blockflow-presets manifest")
    p.add_argument("--endpoint-id", required=True, help="RunPod endpoint to test against")
    p.add_argument("--workflow-timeout", type=int, default=1800,
                   help="Max seconds for the workflow run (default 1800)")
    p.add_argument("--runtime-repo-ref", metavar="REF",
                   help="Override RUNTIME_REPO_REF on the installer pod (used "
                        "pre-merge to test feature branches of serverless-runtime)")
    p.add_argument("--supply-constraint-budget", type=int, default=300,
                   help="Max wall-clock seconds to keep retrying on RunPod CPU "
                        "SUPPLY_CONSTRAINT before exiting 0 with skipped=true "
                        "(default 300 = 5 minutes)")
    args = p.parse_args()

    try:
        result = run_with_retry(
            lambda: smoke(args.preset_id, args.endpoint_id, args.workflow_timeout,
                          runtime_repo_ref=args.runtime_repo_ref),
            max_wall_seconds=args.supply_constraint_budget,
        )
        result.setdefault("preset_id", args.preset_id)
        result.setdefault("endpoint_id", args.endpoint_id)
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
