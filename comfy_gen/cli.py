"""CLI entry point for comfy-gen."""

import argparse
import json
import os
import sys
import urllib.error

from comfy_gen import output


def cmd_config(args: argparse.Namespace) -> None:
    from comfy_gen import config

    if args.set:
        key, _, value = args.set.partition("=")
        if not value:
            output.error(f"Invalid format. Use: --set key=value")
        result = config.set_value(key.strip(), value.strip())
        output.success(result)
    elif args.get:
        value = config.get(args.get)
        if value is None:
            output.error(f"Unknown config key: {args.get}")
        output.success({args.get: value})
    else:
        output.success(config.load())


def cmd_submit(args: argparse.Namespace) -> None:
    from comfy_gen import config, serverless

    cfg = config.load()
    timeout = args.timeout or cfg.get("timeout_seconds", 1200)

    # Parse --override flags: "node_id.param=value"
    overrides: dict[str, dict] = {}
    if args.override:
        for ov in args.override:
            key, _, value = ov.partition("=")
            if not value:
                output.error(f"Invalid override format: {ov}. Use: node_id.param=value")
            node_id, _, param = key.partition(".")
            if not param:
                output.error(f"Invalid override key: {key}. Use: node_id.param=value")
            # Auto-coerce numeric values
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass
            overrides.setdefault(node_id, {})[param] = value

    # Parse --input flags: "node_id=file_path"
    file_inputs: dict[str, str] = {}
    if args.input:
        for inp in args.input:
            node_id, _, path = inp.partition("=")
            if not path:
                output.error(f"Invalid input format: {inp}. Use: node_id=/path/to/file")
            if not os.path.isfile(path):
                output.error(f"Input file not found: {path}")
            file_inputs[node_id] = path

    result = serverless.submit(
        workflow_path=args.workflow,
        file_inputs=file_inputs or None,
        overrides=overrides or None,
        timeout=timeout,
        endpoint_id=getattr(args, "endpoint_id", None),
    )
    print(json.dumps(result))
    sys.exit(1 if not result.get("ok", True) else 0)


def cmd_status(args: argparse.Namespace) -> None:
    from comfy_gen import serverless

    result = serverless.status(args.job_id, endpoint_id=getattr(args, "endpoint_id", None))
    print(json.dumps(result))
    sys.exit(0 if result["status"] not in ("failed", "error") else 1)


def cmd_cancel(args: argparse.Namespace) -> None:
    from comfy_gen import serverless

    result = serverless.cancel(args.job_id, endpoint_id=getattr(args, "endpoint_id", None))
    output.success(result)


def cmd_download(args: argparse.Namespace) -> None:
    from comfy_gen import download

    downloads: list[dict] = []

    if args.batch:
        with open(args.batch) as f:
            downloads = json.load(f)
        if not isinstance(downloads, list):
            output.error("Batch file must contain a JSON array of download specs")
    else:
        if not args.source or not args.target:
            output.error("Usage: comfy-gen download <civitai|url> <version_id|url> [--dest ...]\n  Or:  comfy-gen download --batch <file.json>")
        dl: dict = {"source": args.source, "dest": args.dest}
        if args.source == "civitai":
            dl["version_id"] = args.target
        elif args.source == "url":
            dl["url"] = args.target
            if args.filename:
                dl["filename"] = args.filename
        downloads.append(dl)

    result = download.submit_download(
        downloads=downloads,
        timeout=args.timeout or 600,
        endpoint_id=getattr(args, "endpoint_id", None),
    )
    print(json.dumps(result))
    sys.exit(0)


def cmd_list(args: argparse.Namespace) -> None:
    from comfy_gen import list_models

    result = list_models.submit_list(
        model_type=args.model_type,
        timeout=args.timeout or 60,
        endpoint_id=getattr(args, "endpoint_id", None),
    )
    print(json.dumps(result))
    sys.exit(0)


def cmd_info(args: argparse.Namespace) -> None:
    from comfy_gen import query_info

    result = query_info.submit_query(
        timeout=args.timeout or 60,
        endpoint_id=getattr(args, "endpoint_id", None),
    )
    print(json.dumps(result))
    sys.exit(0)


def cmd_init(args: argparse.Namespace) -> None:
    from comfy_gen import init
    init.run(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="comfy-gen",
        description=(
            "Agent-first CLI for executing ComfyUI workflows on RunPod serverless.\n"
            "All commands output structured JSON to stdout. Human-readable logs go to stderr.\n"
            "\n"
            "Quick start:\n"
            "  comfy-gen init                                       # First-time setup\n"
            "  comfy-gen submit workflow.json                       # Run a workflow\n"
            "\n"
            "Or configure manually:\n"
            "  comfy-gen config --set runpod_api_key=rpa_...\n"
            "  comfy-gen config --set endpoint_id=<endpoint-id>\n"
            "  comfy-gen config --set aws_access_key_id=AKIA...\n"
            "  comfy-gen config --set aws_secret_access_key=...\n"
            "  comfy-gen submit workflow.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser(
        "init",
        help="Interactive setup wizard — create RunPod endpoint and configure storage",
        description=(
            "Interactive setup wizard for first-time ComfyGen configuration.\n"
            "Creates a RunPod serverless endpoint, network volume, and configures\n"
            "S3-compatible storage for file transfer.\n"
            "\n"
            "What it creates:\n"
            "  - Network volume for your ComfyUI models (200GB default)\n"
            "  - Serverless endpoint with GPU tier of your choice\n"
            "  - S3-compatible storage configuration for file transfer\n"
            "\n"
            "All resources are created in your RunPod account. You can manage\n"
            "them later via the RunPod dashboard.\n"
            "\n"
            "GPU tiers:\n"
            "  1. Budget      — RTX 5090 (32GB) in EU-RO-1\n"
            "  2. Recommended — RTX PRO 6000 / A100 SXM (96/80GB) in EUR-IS-1\n"
            "  3. Performance — H100 NVL / H100 PCIe (94/80GB) in US-KS-2\n"
            "\n"
            "Non-interactive mode (for automation):\n"
            "  comfy-gen init --api-key rpa_... --tier 2 \\\n"
            "    --s3-access-key AKIA... --s3-secret-key ... --s3-bucket my-bucket\n"
            "\n"
            "Examples:\n"
            "  comfy-gen init                    # Interactive setup wizard\n"
            "  comfy-gen init --force            # Re-initialize (creates new resources)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_init.add_argument("--force", action="store_true", help="Re-initialize even if already set up")
    p_init.add_argument("--non-interactive", action="store_true", help="Skip interactive prompts (requires all flags)")
    p_init.add_argument("--api-key", metavar="KEY", help="RunPod API key")
    p_init.add_argument("--tier", type=int, choices=[1, 2, 3], help="GPU tier (1=Budget, 2=Recommended, 3=Performance)")
    p_init.add_argument("--volume-size", type=int, default=200, help="Network volume size in GB (default: 200)")
    p_init.add_argument("--s3-access-key", metavar="KEY", help="S3 access key ID")
    p_init.add_argument("--s3-secret-key", metavar="KEY", help="S3 secret access key")
    p_init.add_argument("--s3-bucket", metavar="NAME", help="S3 bucket name")
    p_init.add_argument("--s3-region", metavar="REGION", default="eu-west-2", help="S3 region (default: eu-west-2)")
    p_init.add_argument("--s3-endpoint-url", metavar="URL", help="Custom S3 endpoint for R2/B2/MinIO")
    p_init.add_argument("--civitai-token", metavar="TOKEN", help="CivitAI API token for model downloads")

    # config
    p_config = subparsers.add_parser(
        "config",
        help="Manage persistent configuration (API keys, endpoint, S3 credentials)",
        description=(
            "Read and write persistent configuration stored at ~/.comfy-gen/config.json.\n"
            "Without arguments, prints all current config values as JSON.\n"
            "\n"
            "Available config keys:\n"
            "  runpod_api_key         RunPod API key (rpa_...)\n"
            "  endpoint_id            RunPod serverless endpoint ID\n"
            "  aws_access_key_id      Access key (S3/R2/B2/etc.)\n"
            "  aws_secret_access_key  Secret key (S3/R2/B2/etc.)\n"
            "  s3_region              S3 region (default: eu-west-2)\n"
            "  s3_bucket              Bucket name\n"
            "  s3_endpoint_url        Custom endpoint for R2/B2/MinIO/etc.\n"
            "  timeout_seconds        Max wait for workflow completion (default: 600)\n"
            "  poll_interval_seconds  How often to check job status (default: 3)\n"
            "\n"
            "Storage: S3-compatible (AWS, Cloudflare R2, Backblaze B2, MinIO, DO Spaces)\n"
            "\n"
            "Config is also read from environment variables:\n"
            "  RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID, AWS_ACCESS_KEY_ID,\n"
            "  AWS_SECRET_ACCESS_KEY, S3_REGION, S3_BUCKET, S3_ENDPOINT_URL\n"
            "\n"
            "Priority: config.json > .env file > environment variables > defaults\n"
            "\n"
            "Examples:\n"
            "  comfy-gen config                                     # Show all config\n"
            "  comfy-gen config --set runpod_api_key=rpa_abc123     # Set API key\n"
            "  comfy-gen config --set endpoint_id=abc123def456      # Set endpoint\n"
            "  comfy-gen config --get endpoint_id                   # Get a single value\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_config.add_argument("--set", metavar="KEY=VALUE", help="Set a config value (e.g. --set endpoint_id=abc123)")
    p_config.add_argument("--get", metavar="KEY", help="Get a single config value by key name")
    p_config.add_argument("--list", action="store_true", help="List all config values (same as running with no args)")

    # submit
    p_submit = subparsers.add_parser(
        "submit",
        help="Submit a ComfyUI workflow for execution on serverless",
        description=(
            "Submit a ComfyUI workflow to a RunPod serverless endpoint.\n"
            "Uploads input files to S3, submits the workflow, polls for\n"
            "completion, and returns output URLs.\n"
            "\n"
            "The workflow must be in ComfyUI API format (node-ID-keyed JSON).\n"
            "Export from ComfyUI UI via 'Save (API Format)'.\n"
            "\n"
            "LoadImage nodes referencing local file paths are auto-detected\n"
            "and uploaded to S3. Use --input for manual file mapping (e.g. videos).\n"
            "\n"
            "Output JSON fields:\n"
            "  ok               true on success\n"
            "  output.url       Pre-signed S3 URL for the primary output\n"
            "  output.seed      Seed used (if KSampler present)\n"
            "  output.resolution  {width, height} of the output\n"
            "  output.model_hashes  SHA256 hashes of all models used\n"
            "  job_id           RunPod job ID for status tracking\n"
            "  delay_seconds    Time spent waiting in queue\n"
            "  elapsed_seconds  Execution time on the worker\n"
            "\n"
            "Examples:\n"
            "  comfy-gen submit workflow.json\n"
            "  comfy-gen submit workflow.json --input 193=/path/to/ref.jpg\n"
            "  comfy-gen submit workflow.json --override 7.seed=42\n"
            "  comfy-gen submit workflow.json --override 7.seed=42 --override 7.denoise=0.8\n"
            "  comfy-gen submit workflow.json --timeout 300\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_submit.add_argument("workflow", help="Path to ComfyUI workflow JSON file (API format)")
    p_submit.add_argument(
        "--input", action="append", metavar="NODE_ID=FILE_PATH",
        help="Upload a local file for a specific node (e.g. --input 193=/path/to/ref.jpg). Repeatable.",
    )
    p_submit.add_argument(
        "--override", action="append", metavar="NODE_ID.PARAM=VALUE",
        help="Override a workflow parameter (e.g. --override 7.seed=42). Repeatable.",
    )
    p_submit.add_argument("--timeout", type=int, help="Max seconds to wait for completion (default: 600)")
    p_submit.add_argument("--endpoint-id", metavar="ID", help="RunPod endpoint ID (overrides config)")

    # status
    p_status = subparsers.add_parser(
        "status",
        help="Check the status of a submitted job",
        description=(
            "Query the RunPod API for the current status of a job.\n"
            "\n"
            "Output JSON fields:\n"
            "  job_id           RunPod job ID\n"
            "  status           'in_queue', 'in_progress', 'completed', 'failed', 'cancelled'\n"
            "  output           (completed) Worker output with URL, seed, etc.\n"
            "  delay_seconds    (completed) Queue wait time\n"
            "  elapsed_seconds  (completed) Execution time\n"
            "  error            (failed) Error message\n"
            "\n"
            "Examples:\n"
            "  comfy-gen status abc-123-def-456\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_status.add_argument("job_id", help="RunPod job ID (returned by 'submit' command)")
    p_status.add_argument("--endpoint-id", metavar="ID", help="RunPod endpoint ID (overrides config)")

    # download
    p_download = subparsers.add_parser(
        "download",
        help="Download models to the RunPod network volume",
        description=(
            "Download model files to your RunPod network volume via a serverless job.\n"
            "Supports CivitAI (by model version ID) and direct URLs (HuggingFace, etc.).\n"
            "\n"
            "The download runs on a serverless worker with the network volume mounted,\n"
            "so files land directly at /runpod-volume/ComfyUI/models/<dest>/.\n"
            "\n"
            "Supported --dest values (subfolder under models/):\n"
            "  checkpoints        SD, SDXL, Flux, Wan, etc.\n"
            "  loras              LoRA models\n"
            "  vae                VAE models\n"
            "  clip               CLIP models\n"
            "  diffusion_models   Diffusion model weights\n"
            "  text_encoders      Text encoder weights\n"
            "  controlnet         ControlNet models\n"
            "  upscale_models     Upscaler models\n"
            "\n"
            "CivitAI downloads use the model VERSION ID (not model ID).\n"
            "Find it on CivitAI: model page → version → the number in the URL.\n"
            "\n"
            "Output JSON fields:\n"
            "  ok                 true on success\n"
            "  files              Array of downloaded files with filename, dest, path, size_mb\n"
            "  job_id             RunPod job ID\n"
            "  elapsed_seconds    Total download time\n"
            "\n"
            "Examples:\n"
            "  comfy-gen download civitai 456789 --dest loras\n"
            "  comfy-gen download url https://huggingface.co/org/repo/resolve/main/model.safetensors --dest checkpoints\n"
            "  comfy-gen download url https://huggingface.co/org/repo/resolve/main/model.safetensors --dest checkpoints --filename my_model.safetensors\n"
            "  comfy-gen download --batch downloads.json\n"
            "\n"
            "Batch file format (JSON array):\n"
            "  [\n"
            '    {"source": "civitai", "version_id": "456789", "dest": "loras"},\n'
            '    {"source": "url", "url": "https://...", "dest": "checkpoints"}\n'
            "  ]\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_download.add_argument(
        "source", nargs="?", choices=["civitai", "url"],
        help="Download source: 'civitai' (model version ID) or 'url' (direct URL)",
    )
    p_download.add_argument(
        "target", nargs="?",
        help="CivitAI model version ID or direct download URL",
    )
    p_download.add_argument(
        "--dest", default="checkpoints",
        help="Model subfolder under /runpod-volume/ComfyUI/models/ (default: checkpoints)",
    )
    p_download.add_argument(
        "--filename", help="Output filename (URL mode only; derived from URL if omitted)",
    )
    p_download.add_argument(
        "--timeout", type=int, help="Max seconds to wait for completion (default: 600)",
    )
    p_download.add_argument(
        "--batch", metavar="FILE",
        help="Path to JSON file with array of download specs (overrides positional args)",
    )
    p_download.add_argument("--endpoint-id", metavar="ID", help="RunPod endpoint ID (overrides config)")

    # cancel
    p_cancel = subparsers.add_parser(
        "cancel",
        help="Cancel a running or queued job",
        description=(
            "Cancel a running or queued serverless job via the RunPod API.\n"
            "\n"
            "Examples:\n"
            "  comfy-gen cancel abc-123-def-456\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_cancel.add_argument("job_id", help="RunPod job ID to cancel")
    p_cancel.add_argument("--endpoint-id", metavar="ID", help="RunPod endpoint ID (overrides config)")

    # list
    p_list = subparsers.add_parser(
        "list",
        help="List model files on the RunPod network volume",
        description=(
            "List model files installed on the RunPod network volume by submitting\n"
            "a lightweight job to the serverless endpoint. Scans both the baked-in\n"
            "ComfyUI models directory and the network volume, plus any paths from\n"
            "extra_model_paths.yaml.\n"
            "\n"
            "Supported model types (subfolder under models/):\n"
            "  loras              LoRA models (default)\n"
            "  checkpoints        SD, SDXL, Flux, Wan, etc.\n"
            "  vae                VAE models\n"
            "  clip               CLIP models\n"
            "  diffusion_models   Diffusion model weights\n"
            "  text_encoders      Text encoder weights\n"
            "  controlnet         ControlNet models\n"
            "  upscale_models     Upscaler models\n"
            "  embeddings         Text embeddings\n"
            "\n"
            "Output JSON fields:\n"
            "  ok                 true on success\n"
            "  model_type         The model type queried\n"
            "  files              Array of {filename, path, size_mb}\n"
            "  search_paths       Directories that were scanned\n"
            "  job_id             RunPod job ID\n"
            "\n"
            "Examples:\n"
            "  comfy-gen list loras\n"
            "  comfy-gen list checkpoints\n"
            "  comfy-gen list diffusion_models --endpoint-id abc123\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_list.add_argument(
        "model_type", nargs="?", default="loras",
        help="Model type to list (default: loras)",
    )
    p_list.add_argument(
        "--timeout", type=int, help="Max seconds to wait for completion (default: 60)",
    )
    p_list.add_argument("--endpoint-id", metavar="ID", help="RunPod endpoint ID (overrides config)")

    # info
    p_info = subparsers.add_parser(
        "info",
        help="Query available samplers, schedulers, and LoRAs from the endpoint",
        description=(
            "Query the remote ComfyUI instance for all dynamic configuration values.\n"
            "Returns available samplers, schedulers, and installed LoRA models in a\n"
            "single response. These are consolidated because they are dynamic options\n"
            "that the BlockFlow UI needs to populate dropdowns and selectors.\n"
            "\n"
            "Output JSON fields:\n"
            "  ok                 true on success\n"
            "  samplers           Array of available sampler names\n"
            "  schedulers         Array of available scheduler names\n"
            "  loras              Array of {filename, path, size_mb}\n"
            "  job_id             RunPod job ID\n"
            "\n"
            "Examples:\n"
            "  comfy-gen info\n"
            "  comfy-gen info --endpoint-id abc123\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_info.add_argument(
        "--timeout", type=int, help="Max seconds to wait for completion (default: 60)",
    )
    p_info.add_argument("--endpoint-id", metavar="ID", help="RunPod endpoint ID (overrides config)")

    args = parser.parse_args()

    try:
        {
            "init": cmd_init,
            "config": cmd_config,
            "submit": cmd_submit,
            "download": cmd_download,
            "status": cmd_status,
            "cancel": cmd_cancel,
            "list": cmd_list,
            "info": cmd_info,
        }[args.command](args)
    except ValueError as e:
        output.error(str(e))
    except FileNotFoundError as e:
        output.error(str(e))
    except RuntimeError as e:
        output.error(str(e))
    except ConnectionError as e:
        output.error(f"Connection failed: {e}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            output.error(f"HTTP 401 Unauthorized. Check your RunPod API key.")
        elif e.code == 404:
            output.error(f"HTTP 404 Not Found. Check your endpoint ID.")
        else:
            output.error(f"HTTP {e.code} at {e.url}: {e.reason}")
    except urllib.error.URLError as e:
        output.error(f"Network error: {e.reason}. Check your internet connection.")
    except json.JSONDecodeError as e:
        output.error(f"Invalid JSON in workflow file: {e}")
    except KeyboardInterrupt:
        output.error("Interrupted")
    except Exception as e:
        output.error(f"Unexpected error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
