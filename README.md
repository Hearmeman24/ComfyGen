# comfy-gen

**An agentic CLI for running ComfyUI workflows on RunPod serverless.** Designed to be used by AI coding agents (Claude Code, Cursor, Codex, Windsurf, Gemini CLI) as much as by humans. All output is structured JSON. No interactive prompts. Every command has verbose `--help` so agents can discover capabilities without documentation.

## Table of Contents

- [Use with Your Favorite AI Agent](#use-with-your-favorite-ai-agent)
- [How It Works](#how-it-works)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Commands](#commands)
  - [submit](#comfy-gen-submit-workflowjson)
  - [status](#comfy-gen-status-job-id)
  - [cancel](#comfy-gen-cancel-job-id)
  - [download](#comfy-gen-download-civitaiurl-target)
  - [config](#comfy-gen-config)
- [Getting Models onto Your Volume](#getting-models-onto-your-volume)
- [Configuration](#configuration)
- [Storage](#storage)
- [Workflow Format](#workflow-format)
- [Output Format](#output-format)
- [Prerequisites](#prerequisites)

## Use with Your Favorite AI Agent

ComfyGen ships with an **agent skill file** at `SKILL.md` in the repo root. Drop it into your agent's skill/tool directory and it can submit workflows, download models, check job status, and manage your RunPod infrastructure — all through natural language.

```
You: "Submit this workflow with seed 42 and download the output"
Agent: runs comfy-gen submit workflow.json --override 7.seed=42, parses JSON, fetches URL
```

Works with any agent that can run shell commands and parse JSON. The skill file teaches your agent the full API surface, including workflow analysis, parameter overrides, and error handling.

## How It Works

```
comfy-gen submit workflow.json
```

1. Detects local file references in your workflow and uploads them to S3
2. Submits the workflow to your RunPod serverless endpoint
3. Polls for completion with real-time progress
4. Returns a JSON result with output URLs

Workers spin up on demand, execute the workflow, and shut down. You pay only for execution time.

## Installation

Requires Python 3.11+.

### Recommended: pipx (installs system-wide, no venv needed)

```bash
brew install pipx        # macOS — or: apt install pipx / pip install pipx
pipx ensurepath          # adds pipx bin dir to PATH (restart your shell after)

git clone https://github.com/Hearmeman24/ComfyGen.git
cd ComfyGen
pipx install --editable .
pipx inject comfy-gen boto3
```

### Alternative: pip

```bash
git clone https://github.com/Hearmeman24/ComfyGen.git
cd ComfyGen
pip install -e .
pip install boto3
```

After installation, `comfy-gen` is available system-wide as a CLI command.

> **Windows users:** Make sure "Add Python to PATH" was checked during Python installation, or `comfy-gen` won't be found. If you missed it, add Python's `Scripts` directory to your PATH manually (e.g. `C:\Users\<you>\AppData\Local\Programs\Python\Python3xx\Scripts`).

## Quick Start

```bash
# 1. Run the setup wizard (creates RunPod endpoint + configures storage)
comfy-gen init

# 2. Download models to your network volume
comfy-gen download civitai 456789 --dest loras
comfy-gen download url https://huggingface.co/org/repo/resolve/main/model.safetensors --dest checkpoints

# 3. Submit a workflow
comfy-gen submit workflow.json

# 4. Submit with an input image for a LoadImage node
comfy-gen submit workflow.json --input 193=/path/to/photo.jpg

# 5. Override workflow parameters
comfy-gen submit workflow.json --override 7.seed=42 --override 7.denoise=0.8

# 6. Check job status / cancel
comfy-gen status <job-id>
comfy-gen cancel <job-id>
```

## Commands

### `comfy-gen submit <workflow.json>`

Full pipeline: upload inputs, submit to serverless, poll, return output URLs.

```bash
comfy-gen submit workflow.json
comfy-gen submit workflow.json --input 193=/path/to/ref.jpg
comfy-gen submit workflow.json --override 7.seed=42
comfy-gen submit workflow.json --timeout 300
```

**Progress (stderr):**
```
Submitting to RunPod serverless endpoint...
Job submitted: abc-123-def
[3s]  IN_QUEUE
[6s]  node_check: Checking custom nodes (10%)
[9s]  inference: (2/8) Step 1/8 (29%)
[12s] inference: (5/8) Step 4/8 (55%)
[15s] upload: Uploading outputs to S3 (92%)
Completed in 27s (+2s queue). 1 image
```

**Result (stdout):**
```json
{
  "ok": true,
  "output": {
    "url": "https://bucket.s3.region.amazonaws.com/comfy-gen/outputs/abc123.png",
    "seed": 1027836870258818,
    "resolution": {"width": 828, "height": 1248},
    "model_hashes": {
      "model.safetensors": {"sha256": "240761...", "type": "diffusion_models"},
      "lora.safetensors": {"sha256": "2fdc9d...", "type": "loras", "strength": 0.8}
    }
  },
  "job_id": "abc-123-def",
  "delay_seconds": 2,
  "elapsed_seconds": 27
}
```

**Features:**
- Auto-detects `LoadImage` nodes referencing local files and uploads them to S3
- `--input NODE_ID=FILE_PATH` for explicit file mapping (videos, etc.) — repeatable
- `--override NODE_ID.PARAM=VALUE` for parameter overrides — repeatable, auto-coerces numbers
- Real-time progress with stage, step count, and percentage
- Output metadata stripped (no embedded workflow data in images)
- Returns model hashes (SHA256), types, and LoRA strengths

### `comfy-gen status <job-id>`

Check the status of a submitted job.

```json
{"job_id": "abc-123", "status": "completed", "ok": true, "output": {"url": "..."}, "elapsed_seconds": 27, "delay_seconds": 2}
```

### `comfy-gen cancel <job-id>`

Cancel a running or queued job.

### `comfy-gen download <civitai|url> <target>`

Download models to your RunPod network volume via a serverless job. Files land directly on the mounted volume at `/runpod-volume/ComfyUI/models/<dest>/`.

```bash
# Download a LoRA from CivitAI (use model VERSION ID, not model ID)
comfy-gen download civitai 456789 --dest loras

# Download a checkpoint from HuggingFace
comfy-gen download url https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors --dest checkpoints

# Download with a custom filename
comfy-gen download url https://example.com/model.safetensors --dest diffusion_models --filename my_model.safetensors

# Batch download from a JSON file
comfy-gen download --batch models.json
```

**Supported `--dest` values:** `checkpoints`, `loras`, `vae`, `clip`, `diffusion_models`, `text_encoders`, `controlnet`, `upscale_models`

**Batch file format:**
```json
[
  {"source": "civitai", "version_id": "456789", "dest": "loras"},
  {"source": "url", "url": "https://huggingface.co/.../model.safetensors", "dest": "checkpoints"}
]
```

**Result:**
```json
{
  "ok": true,
  "files": [
    {"filename": "model.safetensors", "dest": "loras", "path": "/runpod-volume/ComfyUI/models/loras/model.safetensors", "size_mb": 228.5}
  ],
  "job_id": "abc-123-def",
  "elapsed_seconds": 45
}
```

## Getting Models onto Your Volume

There are two ways to populate your RunPod network volume with models:

### Option 1: `comfy-gen download` (individual models)

Download specific models from CivitAI or HuggingFace directly to your volume. Best for adding individual LoRAs, checkpoints, or other files on demand. See the [download command](#comfy-gen-download-civitaiurl-target) above.

### Option 2: Pre-populated RunPod Templates (full model sets)

For common workflows, deploy a [HearmemanAI RunPod template](https://get.runpod.io/wan-template) that comes pre-configured to download entire model sets to your network volume on first boot.

For example, to get all models needed for **Wan 2.2 + Wan Animate** video generation:

1. Deploy the [Wan template](https://get.runpod.io/wan-template) as a **GPU pod** attached to your network volume
2. Set these environment variables on the pod:
   - `download_wan22=true`
   - `download_wan_animate=true`
3. Wait for the pod to fully deploy — all Wan 2.2 and Wan Animate models will be downloaded to the volume
4. Stop and delete the pod — the models persist on your network volume
5. Your serverless endpoint (which mounts the same volume) can now run Wan workflows

This is the fastest way to get started with a specific workflow type. You only need to deploy the template once per model set.

---

### `comfy-gen config`

Manage persistent configuration stored at `~/.comfy-gen/config.json`.

```bash
comfy-gen config                                     # Show all config
comfy-gen config --set runpod_api_key=rpa_abc123     # Set a value
comfy-gen config --get endpoint_id                   # Get a single value
```

## Configuration

Config is read from multiple sources with this priority order:

**config.json > .env file > environment variables > defaults**

| Key | Description | Env Var | Default |
|-----|-------------|---------|---------|
| `runpod_api_key` | RunPod API key | `RUNPOD_API_KEY` | — |
| `endpoint_id` | RunPod serverless endpoint ID | `RUNPOD_ENDPOINT_ID` | — |
| `aws_access_key_id` | S3 access key | `AWS_ACCESS_KEY_ID` | — |
| `aws_secret_access_key` | S3 secret key | `AWS_SECRET_ACCESS_KEY` | — |
| `s3_bucket` | S3 bucket name | `S3_BUCKET` | — |
| `s3_region` | S3 region | `S3_REGION` | `eu-west-2` |
| `s3_endpoint_url` | Custom S3 endpoint (for R2/B2/MinIO) | `S3_ENDPOINT_URL` | — |
| `civitai_token` | CivitAI API token for model downloads | `CIVITAI_TOKEN` | — |
| `timeout_seconds` | Max wait for completion | `COMFY_GEN_TIMEOUT` | `600` |
| `poll_interval_seconds` | Status poll interval | `COMFY_GEN_POLL_INTERVAL` | `3` |

You can also put these in a `.env` file in your project directory.

## Storage

ComfyGen requires **S3-compatible storage** for transferring input files (images, videos) to workers and receiving output files. The setup wizard (`comfy-gen init`) configures storage and verifies it works before creating your endpoint.

| Service | Config |
|---------|--------|
| **AWS S3** | Set `aws_access_key_id`, `aws_secret_access_key`, `s3_bucket`, `s3_region` |
| **Cloudflare R2** | Same as above + `s3_endpoint_url=https://<account-id>.r2.cloudflarestorage.com`, `s3_region=auto` |
| **Backblaze B2** | Same as above + `s3_endpoint_url=https://s3.<region>.backblazeb2.com` |
| **MinIO** | Same as above + `s3_endpoint_url=http://your-minio:9000` |
| **DigitalOcean Spaces** | Same as above + `s3_endpoint_url=https://<region>.digitaloceanspaces.com` |

Uploads are content-addressed (MD5 hash key) — identical files are never re-uploaded.

## Workflow Format

Workflows must be in **ComfyUI API format** — the node-ID-keyed JSON with `class_type` and `inputs` fields. Export from ComfyUI via **Save (API Format)** or enable Dev Mode first.

```json
{
  "7": {
    "inputs": {"seed": 42, "steps": 20, "cfg": 7.0, "model": ["10", 0]},
    "class_type": "KSampler"
  },
  "10": {
    "inputs": {"ckpt_name": "model.safetensors"},
    "class_type": "CheckpointLoaderSimple"
  }
}
```

## Output Format

All commands output JSON to **stdout**. Human-readable progress goes to **stderr**. This makes comfy-gen composable with `jq`, shell scripts, and AI agents.

```bash
# Extract just the output URL
comfy-gen submit workflow.json 2>/dev/null | jq -r '.output.url'

# Save output URL to a variable
URL=$(comfy-gen submit workflow.json 2>/dev/null | jq -r '.output.url')
```

**Success** exits with code `0`:
```json
{"ok": true, "output": {"url": "...", "seed": 42, "resolution": {"width": 1024, "height": 1024}}, "job_id": "...", "elapsed_seconds": 30}
```

**Error** exits with code `1`:
```json
{"status": "error", "error": "No RunPod API key configured. Set via: comfy-gen config --set runpod_api_key=rpa_..."}
```

## Prerequisites

You need:
1. A **RunPod account** — `comfy-gen init` creates the serverless endpoint for you
2. **S3-compatible storage** — AWS S3, Cloudflare R2, Backblaze B2, or any S3-compatible service (see [Storage](#storage))
3. **Python 3.11+** and `boto3` (`pipx inject comfy-gen boto3` or `pip install boto3`)

## License

MIT
