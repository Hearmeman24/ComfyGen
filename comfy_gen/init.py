"""Interactive setup wizard for ComfyGen.

Creates RunPod serverless infrastructure (network volume + endpoint)
and configures S3 storage. This is the only interactive command in
the CLI — all other commands are agent-first / non-interactive.
"""

import argparse
import getpass
import json
import sys
import time
from typing import Any

from comfy_gen import config, output, runpod_api

BANNER = r"""
   ______                 __       ______
  / ____/___  ____ ___  / __/_  _/ ____/__  ____
 / /   / __ \/ __ `__ \/ /_/ / / / / __/ _ \/ __ \
/ /___/ /_/ / / / / / / __/ /_/ / /_/ /  __/ / / /
\____/\____/_/ /_/ /_/_/  \__, /\____/\___/_/ /_/
                         /____/
                              by HearmemanAI
"""

TIERS: dict[str, dict[str, Any]] = {
    "1": {
        "name": "Budget",
        "gpu_ids": ["NVIDIA GeForce RTX 5090"],
        "datacenter": "EU-RO-1",
        "label": "RTX 5090 (32GB)",
        "region": "Europe — Romania",
    },
    "2": {
        "name": "Recommended",
        "gpu_ids": [
            "NVIDIA RTX PRO 6000 Blackwell Server Edition",
            "NVIDIA A100-SXM4-80GB",
        ],
        "datacenter": "EUR-IS-1",
        "label": "RTX PRO 6000 / A100 SXM (96/80GB)",
        "region": "Europe — Iceland",
    },
    "3": {
        "name": "Performance",
        "gpu_ids": ["NVIDIA H100 NVL", "NVIDIA H100 PCIe"],
        "datacenter": "US-KS-2",
        "label": "H100 NVL / H100 PCIe (94/80GB)",
        "region": "US — Kansas",
    },
}

DEFAULT_VOLUME_SIZE = 200


def _log(msg: str = "") -> None:
    """Print to stderr (never pollutes JSON stdout)."""
    print(msg, file=sys.stderr)


def _prompt(label: str, default: str = "", hidden: bool = False) -> str:
    """Prompt user for input on stderr, read from stdin."""
    if default:
        prompt_text = f"  {label} [{default}]: "
    else:
        prompt_text = f"  {label}: "

    if hidden:
        print(prompt_text, end="", file=sys.stderr, flush=True)
        value = getpass.getpass(prompt="")
    else:
        print(prompt_text, end="", file=sys.stderr, flush=True)
        value = input()

    return value.strip() or default


def _choose(label: str, options: list[str], valid: list[str]) -> str:
    """Prompt user to choose from numbered options."""
    while True:
        print(f"  {label}: ", end="", file=sys.stderr, flush=True)
        choice = input().strip()
        if choice in valid:
            return choice
        _log(f"  Invalid choice. Enter one of: {', '.join(valid)}")


def _test_storage(s3_config: dict[str, str]) -> None:
    """Upload a test file to S3, download it back, and verify contents match."""
    import tempfile
    import urllib.request

    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise RuntimeError(
            "boto3 is required for S3 storage. Install via: pip install boto3"
        )

    client_kwargs = {
        "region_name": s3_config.get("s3_region", "eu-west-2"),
        "aws_access_key_id": s3_config["aws_access_key_id"],
        "aws_secret_access_key": s3_config["aws_secret_access_key"],
        "config": Config(signature_version="s3v4"),
    }
    endpoint_url = s3_config.get("s3_endpoint_url", "")
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url

    client = boto3.client("s3", **client_kwargs)
    bucket = s3_config["s3_bucket"]
    test_key = "comfy-gen/.storage-test"
    test_data = b"comfy-gen storage test"

    # Upload
    client.put_object(Bucket=bucket, Key=test_key, Body=test_data)

    # Download via pre-signed URL (same path the worker uses)
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": test_key},
        ExpiresIn=60,
    )
    with tempfile.NamedTemporaryFile() as tmp:
        urllib.request.urlretrieve(url, tmp.name)
        downloaded = open(tmp.name, "rb").read()

    if downloaded != test_data:
        raise RuntimeError("Downloaded content does not match uploaded content")

    # Clean up
    client.delete_object(Bucket=bucket, Key=test_key)


def run(args: argparse.Namespace) -> None:
    """Run the init wizard."""
    non_interactive = getattr(args, "non_interactive", False)

    # Check for existing init
    if config.is_initialized():
        if non_interactive:
            output.error("Already initialized. Use --force to re-initialize.")
        _log("\n  ComfyGen is already initialized.")
        _log("  Run with --force to re-initialize (creates new resources).\n")
        if not getattr(args, "force", False):
            existing = config.load_init()
            output.success(existing)
        _log("  Proceeding with re-initialization...\n")

    # ── Banner ──
    if not non_interactive:
        _log(BANNER)
        _log("  Welcome to ComfyGen setup. This will create a RunPod serverless")
        _log("  endpoint for running ComfyUI workflows.\n")

    # ── Step 1: RunPod API Key ──
    if not non_interactive:
        _log("─── Step 1: RunPod API Key ───────────────────────────────────\n")

    api_key = getattr(args, "api_key", None)
    if not api_key:
        if non_interactive:
            output.error("--api-key is required in non-interactive mode.")
        api_key = _prompt("RunPod API key", hidden=True)

    if not api_key:
        output.error("No API key provided.")

    _log("  Validating API key...")
    if not runpod_api.validate_api_key(api_key):
        output.error("Invalid RunPod API key. Check your key at https://www.runpod.io/console/user/settings")

    if not non_interactive:
        _log("  ✓ API key valid\n")

    # ── Step 2: Storage ──
    if not non_interactive:
        _log("─── Step 2: Storage ──────────────────────────────────────────\n")
        _log("  ComfyGen needs S3-compatible storage for transferring files")
        _log("  between your machine and the serverless workers.\n")
        _log("  Supported providers:")
        _log("    • AWS S3")
        _log("    • Cloudflare R2")
        _log("    • Backblaze B2")
        _log("    • MinIO / any S3-compatible service\n")
    s3_config: dict[str, str] = {}

    # Check if S3 args were provided in non-interactive mode
    has_s3_args = getattr(args, "s3_access_key", None) and getattr(args, "s3_secret_key", None)

    if non_interactive and not has_s3_args:
        output.error("S3 storage is required. Provide --s3-access-key, --s3-secret-key, and --s3-bucket.")
    elif non_interactive and has_s3_args:
        s3_config = {
            "aws_access_key_id": args.s3_access_key,
            "aws_secret_access_key": args.s3_secret_key,
            "s3_bucket": getattr(args, "s3_bucket", "") or "",
            "s3_region": getattr(args, "s3_region", "eu-west-2") or "eu-west-2",
            "s3_endpoint_url": getattr(args, "s3_endpoint_url", "") or "",
        }
        if not s3_config["s3_bucket"]:
            output.error("--s3-bucket is required when configuring S3 storage.")
    else:
        while True:
            _log()
            _log("  You'll need an API token from your storage provider.")
            _log("  For Cloudflare R2: Dashboard → R2 → Manage R2 API Tokens\n")
            s3_config["aws_access_key_id"] = _prompt("Access Key ID")
            s3_config["aws_secret_access_key"] = _prompt("Secret Access Key", hidden=True)
            s3_config["s3_bucket"] = _prompt("Bucket name")
            _log()
            _log("  For AWS S3, the region is e.g. 'us-east-1' or 'eu-west-2'.")
            _log("  For Cloudflare R2, enter 'auto'.\n")
            s3_config["s3_region"] = _prompt("Region", default="auto")
            _log()
            _log("  Endpoint URL is required for non-AWS providers:")
            _log("    Cloudflare R2:  https://<account-id>.r2.cloudflarestorage.com")
            _log("    Backblaze B2:   https://s3.<region>.backblazeb2.com")
            _log("    MinIO:          http://your-minio:9000")
            _log("    AWS S3:         leave empty\n")
            s3_config["s3_endpoint_url"] = _prompt(
                "Endpoint URL (empty for AWS S3)", default=""
            )
            if not s3_config["aws_access_key_id"] or not s3_config["aws_secret_access_key"]:
                _log("  ✗ Access key and secret key are required. Try again.\n")
                continue
            if not s3_config["s3_bucket"]:
                _log("  ✗ Bucket name is required. Try again.\n")
                continue

            # Test the connection
            _log()
            _log("  Testing storage connection...")
            try:
                _test_storage(s3_config)
                _log("  ✓ Storage test passed — upload and download verified\n")
                break
            except Exception as e:
                _log(f"\n  ✗ Storage test failed: {e}")
                _log("  Check your credentials, bucket name, and endpoint URL.")
                retry = _prompt("Try again? [Y/n]", default="Y")
                if retry.lower() in ("n", "no"):
                    output.error(f"Storage test failed: {e}")

    # Verify storage in non-interactive mode (no retry)
    if non_interactive and s3_config:
        _log("  Testing storage connection...")
        try:
            _test_storage(s3_config)
            _log("  ✓ Storage test passed\n")
        except Exception as e:
            output.error(f"Storage test failed: {e}")

    # ── Step 3: CivitAI Token (optional) ──
    civitai_token = getattr(args, "civitai_token", None) or ""
    if not non_interactive:
        _log("─── Step 3: CivitAI Token (optional) ─────────────────────────\n")
        _log("  A CivitAI API token lets you download models from CivitAI")
        _log("  directly to your network volume using 'comfy-gen download'.")
        _log("  Without it, you can still download from HuggingFace and")
        _log("  other direct URLs.\n")
        _log("  Get your token at: https://civitai.com/user/account\n")
        civitai_token = _prompt("CivitAI API token (press Enter to skip)", hidden=True)
        if civitai_token:
            _log("  ✓ CivitAI token saved\n")
        else:
            _log("  Skipped. Set later via: comfy-gen config --set civitai_token=...\n")

    # ── Step 4: GPU Tier ──
    if not non_interactive:
        _log("─── Step 4: Select GPU Tier ──────────────────────────────────\n")
        for key, tier in TIERS.items():
            _log(f"  [{key}] {tier['name']:<14} — {tier['label']}")
            _log(f"      {tier['region']}")
        _log()

    tier_choice = getattr(args, "tier", None)
    if tier_choice:
        tier_choice = str(tier_choice)
    if tier_choice not in TIERS:
        if non_interactive:
            output.error(f"--tier must be 1, 2, or 3. Got: {tier_choice}")
        tier_choice = _choose("Select tier [1/2/3]", list(TIERS.keys()), list(TIERS.keys()))

    tier = TIERS[tier_choice]
    if not non_interactive:
        _log(f"\n  Selected: {tier['name']} — {tier['label']}\n")

    # ── Step 5: Network Volume ──
    if not non_interactive:
        _log("─── Step 5: Network Volume ───────────────────────────────────\n")

    volume_size = getattr(args, "volume_size", None) or DEFAULT_VOLUME_SIZE
    if not non_interactive:
        size_input = _prompt(f"Volume size in GB", default=str(DEFAULT_VOLUME_SIZE))
        try:
            volume_size = int(size_input)
        except ValueError:
            output.error(f"Invalid volume size: {size_input}")
        if volume_size < 10:
            output.error("Minimum volume size is 10GB.")

    _log(f"  Creating {volume_size}GB network volume in {tier['datacenter']}...")
    try:
        volume = runpod_api.create_network_volume(
            api_key,
            name="comfygen-models",
            size_gb=volume_size,
            datacenter_id=tier["datacenter"],
        )
    except RuntimeError as e:
        output.error(f"Failed to create network volume: {e}")

    volume_id = volume["id"]
    if not non_interactive:
        _log(f"  ✓ Volume created: {volume_id} ({volume_size}GB, {tier['datacenter']})\n")
        _log("  Your models go on this volume at /runpod-volume/ComfyUI/models/")
        _log("  You can resize it later in the RunPod dashboard.\n")

    # ── Step 6: Create Template + Endpoint ──
    if not non_interactive:
        _log("─── Step 6: Serverless Endpoint ──────────────────────────────\n")

    # Build env vars for the worker template
    template_env: dict[str, str] = {
        "RUNTIME_REPO_URL": runpod_api.RUNTIME_REPO_URL,
        "RUNTIME_REPO_REF": "main",
    }
    if s3_config:
        template_env["AWS_ACCESS_KEY_ID"] = s3_config["aws_access_key_id"]
        template_env["AWS_SECRET_ACCESS_KEY"] = s3_config["aws_secret_access_key"]
        template_env["S3_BUCKET"] = s3_config["s3_bucket"]
        if s3_config.get("s3_region"):
            template_env["S3_REGION"] = s3_config["s3_region"]
        if s3_config.get("s3_endpoint_url"):
            template_env["S3_ENDPOINT_URL"] = s3_config["s3_endpoint_url"]
    if civitai_token:
        template_env["CIVITAI_TOKEN"] = civitai_token

    _log("  Creating serverless template...")
    try:
        template = runpod_api.create_template(
            api_key,
            name="comfygen",
            env=template_env,
        )
    except RuntimeError as e:
        output.error(f"Failed to create template: {e}")

    template_id = template["id"]

    _log("  Creating serverless endpoint...")
    try:
        endpoint = runpod_api.create_endpoint(
            api_key,
            name="comfygen",
            template_id=template_id,
            gpu_type_ids=tier["gpu_ids"],
            volume_id=volume_id,
        )
    except RuntimeError as e:
        output.error(f"Failed to create endpoint: {e}")

    endpoint_id = endpoint["id"]
    if not non_interactive:
        gpu_names = ", ".join(tier["gpu_ids"])
        _log(f"  ✓ Endpoint created: {endpoint_id}")
        _log(f"    Template: {template_id}")
        _log(f"    GPUs: {gpu_names}")
        _log(f"    Workers: 0 min, 3 max (scale to zero)")
        _log(f"    FlashBoot: enabled\n")

    # ── Save Config ──
    cfg = config.load()
    cfg["runpod_api_key"] = api_key
    cfg["endpoint_id"] = endpoint_id
    if civitai_token:
        cfg["civitai_token"] = civitai_token
    if s3_config:
        cfg.update(s3_config)
    config.save(cfg)

    # ── Save Init Marker ──
    init_data = {
        "initialized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint_id": endpoint_id,
        "template_id": template_id,
        "volume_id": volume_id,
        "datacenter": tier["datacenter"],
        "tier": tier["name"],
        "gpu_types": tier["gpu_ids"],
    }
    config.save_init(init_data)

    if not non_interactive:
        _log("  ✓ Config saved to ~/.comfy-gen/config.json\n")

    # ── Step 7: Wait for endpoint readiness ──
    if not non_interactive:
        _log("─── Step 7: Waiting for Endpoint ──────────────────────────────\n")
    _log("  Workers are downloading the Docker image (this takes 15-20 min)...")
    _log("  You can Ctrl+C to skip — the endpoint will finish initializing in the background.\n")
    ready = False
    poll_interval = 15
    max_wait = 1800  # 30 minutes
    elapsed = 0
    last_msg = ""

    try:
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                health = runpod_api.get_endpoint_health(api_key, endpoint_id)
                workers = health.get("workers", {})
                initializing = workers.get("initializing", 0)
                w_ready = workers.get("ready", 0)
                idle = workers.get("idle", 0)

                if w_ready > 0 or idle > 0:
                    ready = True
                    break

                mins = elapsed // 60
                secs = elapsed % 60
                msg = f"  [{mins}m{secs:02d}s] {initializing} worker(s) initializing..."
                if msg != last_msg:
                    _log(msg)
                    last_msg = msg
            except Exception:
                if elapsed % 60 == 0:
                    _log(f"  [{elapsed // 60}m] Waiting for health endpoint...")
    except KeyboardInterrupt:
        _log("\n  Skipped. The endpoint will continue initializing in the background.")
        _log(f"  Monitor at: https://www.runpod.io/console/serverless/{endpoint_id}\n")

    if ready:
        mins = elapsed // 60
        secs = elapsed % 60
        _log(f"  ✓ Endpoint is ready! ({mins}m{secs:02d}s)\n")
    elif elapsed >= max_wait:
        _log(f"  ⚠ Workers still initializing after {elapsed // 60}m.")
        _log("  This is normal for first-time setup. Monitor progress at:")
        _log(f"  https://www.runpod.io/console/serverless/{endpoint_id}\n")

    # ── Summary ──
    if not non_interactive:
        _log("─── Setup Complete ───────────────────────────────────────────\n")
        _log("  Next steps:")
        _log("    1. Download models to your network volume:")
        _log("       comfy-gen download url <huggingface-url> --dest checkpoints")
        _log("    2. Run a workflow:")
        _log("       comfy-gen submit workflow.json\n")

    output.success(init_data)
