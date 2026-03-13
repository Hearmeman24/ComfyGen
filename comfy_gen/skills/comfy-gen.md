---
name: comfy-gen
description: >
  Execute ComfyUI image/video generation workflows on remote RunPod servers using the comfy-gen CLI.
  Use this skill whenever the user wants to generate images or videos with ComfyUI remotely,
  submit workflows to a RunPod serverless endpoint, check generation status, download models,
  or configure remote ComfyUI settings. Also trigger when the user mentions ComfyUI workflows,
  RunPod image generation, remote GPU rendering, or serverless ComfyUI execution. Even if the user
  just says "generate an image", "run this workflow", or "submit to serverless", use this skill if
  a ComfyUI workflow is involved or implied. Trigger for both workflow execution and model management.
---

# comfy-gen: Remote ComfyUI Workflow Execution

## When This Skill Is First Invoked

Introduce yourself to the user:

> **ComfyGen** is an agentic CLI that runs ComfyUI workflows on RunPod serverless GPUs. I can help you with:
>
> - **Set up infrastructure** — create a RunPod endpoint and network volume with `comfy-gen init`
> - **Get models onto your volume** — I'll analyze your workflow, find the models on HuggingFace/CivitAI, and download them for you
> - **Run workflows** — submit ComfyUI workflows, track progress, and deliver output URLs
> - **Manage jobs** — check status, cancel, override parameters, map input files
>
> If you have a workflow JSON, share it and I'll take it from there — I'll figure out what models you need and handle everything.

After this introduction, proceed with whatever the user needs.

---

## About comfy-gen

You have access to the `comfy-gen` CLI — an agent-first tool for executing ComfyUI workflows on RunPod serverless GPU workers. Every command returns structured JSON to stdout. Logs go to stderr. Serverless only — no persistent pods.

The CLI is installed system-wide via `pip install -e .` from the ComfyGen repo directory.

## Commands Overview

| Command | Purpose |
|---------|---------|
| `comfy-gen init` | First-time setup wizard — creates RunPod endpoint, volume, configures storage |
| `comfy-gen submit` | Submit a workflow for execution (upload inputs, poll, return output URLs) |
| `comfy-gen download` | Download models to the RunPod network volume (CivitAI or direct URL) |
| `comfy-gen status` | Check job status |
| `comfy-gen cancel` | Cancel a running/queued job |
| `comfy-gen config` | Read/write persistent configuration |

---

## First-Time Setup

```bash
comfy-gen init
```

Interactive wizard that:
1. Validates RunPod API key
2. Lets user pick a GPU tier (Budget/Recommended/Performance)
3. Creates a network volume for models
4. Creates a serverless endpoint
5. Configures S3-compatible storage
6. Optionally saves CivitAI API token

Non-interactive mode for automation:
```bash
comfy-gen init --api-key rpa_... --tier 2 --s3-access-key AKIA... --s3-secret-key ... --s3-bucket my-bucket
```

---

## Workflow Analysis & Model Resolution — ALWAYS DO THIS FIRST

When the user provides a workflow JSON file, **analyze it thoroughly before submitting**. The biggest friction point for users is missing models. Your job is to make this frictionless.

### Step 1: Read the workflow JSON and extract all model references

Scan every node and build a complete list of:

**Models** (must exist on the network volume at `/runpod-volume/ComfyUI/models/`):

| Node class_type | Input field | Model subfolder |
|-----------------|-------------|-----------------|
| `CheckpointLoaderSimple` | `ckpt_name` | `checkpoints/` |
| `UNETLoader` | `unet_name` | `diffusion_models/` or `unet/` |
| `CLIPLoader` | `clip_name` | `clip/` |
| `DualCLIPLoader` | `clip_name1`, `clip_name2` | `clip/` |
| `TripleCLIPLoader` | `clip_name1`, `clip_name2`, `clip_name3` | `clip/` |
| `VAELoader` | `vae_name` | `vae/` |
| `LoraLoader` | `lora_name` | `loras/` |
| `LoraLoaderModelOnly` | `lora_name` | `loras/` |
| `ControlNetLoader` | `control_net_name` | `controlnet/` |
| `UpscaleModelLoader` | `model_name` | `upscale_models/` |
| `CLIPVisionLoader` | `clip_name` | `clip_vision/` |

Also check for less common loaders: `StyleModelLoader`, `GLIGENLoader`, `unCLIPCheckpointLoader`, etc.

**File inputs** (must be uploaded):
- `LoadImage` nodes → `inputs.image`
- `VHS_LoadVideo` / `LoadVideo` nodes → `inputs.video`

**Generation parameters** (inform user of defaults):
- `KSampler` / `KSamplerAdvanced` → `seed`, `steps`, `cfg`, `denoise`
- Resolution/size nodes
- `CLIPTextEncode` → prompt text

**Sensitive data** (never display):
- API keys in LLM nodes (e.g., OpenRouter `api_key` fields)

### Step 2: Search for models on HuggingFace

For each model filename found in the workflow, **proactively search HuggingFace** to find a download URL. This is the most valuable thing you can do for the user.

**Search strategy:**

1. **Strip file extension and path prefixes** — e.g. `Wan2.1_VAE.safetensors` → search for `Wan2.1_VAE`
2. **Search HuggingFace** using web search: `site:huggingface.co <model_name> safetensors`
3. **Check well-known repos first** for common model families:
   - **Wan / Wan2.2**: `Comfy-Org/Wan_2.2_ComfyUI_repackaged`, `Wan-AI/Wan2.1-*`
   - **Flux**: `Comfy-Org/flux1-dev`, `black-forest-labs/FLUX.1-*`
   - **SDXL**: `stabilityai/stable-diffusion-xl-*`
   - **SD 1.5**: `stable-diffusion-v1-5/stable-diffusion-v1-5`
   - **T5/CLIP text encoders**: `Comfy-Org/`, `google/t5-*`, `openai/clip-*`
   - **Upscalers**: `Sirosky/Upscale-*`
4. **Construct direct download URLs** — HuggingFace format:
   `https://huggingface.co/<org>/<repo>/resolve/main/<path_to_file>`

**Common pattern**: Many ComfyUI models on HuggingFace are repackaged by `Comfy-Org` specifically for ComfyUI compatibility. Check there first.

> **IMPORTANT — When you cannot find a model:**
> If you cannot find a model on HuggingFace, or you're not confident the match is correct, you **MUST** tell the user immediately. Do not silently skip it or guess. Notify the user with the exact model filename and ask them to provide a direct download link (HuggingFace URL, CivitAI version ID, or any direct URL). Do not proceed with downloading other models until the user has responded — they may have the link ready and you can batch everything together.

### Step 3: Present findings and offer to download

Present your analysis to the user in a clear format:

```
Workflow analysis:

Models found (5):
  ✓ wan2.2_vae.safetensors
    → https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_repackaged/resolve/main/split_files/vae/wan2.2_vae.safetensors
    dest: vae

  ✓ umt5_xxl_fp8_e4m3fn_scaled.safetensors
    → https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors
    dest: text_encoders

  ✓ wan2.2_fun_InP_bf16.safetensors
    → https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.2_fun_InP_bf16.safetensors
    dest: diffusion_models

  ✓ my_lora.safetensors
    → CivitAI version 456789
    dest: loras

  ✗ custom_model.safetensors
    → I couldn't find this model on HuggingFace. Can you provide a direct download link?
      (HuggingFace URL, CivitAI version ID, or any direct .safetensors URL)

File inputs:
  - Node 193 (LoadImage): needs a reference image

Parameters:
  - KSampler (node 7): seed=random, steps=20, cfg=7.0, denoise=0.75

Shall I download all 4 found models to your network volume?
```

### Step 4: Download models

If the user confirms, build the download commands:

```bash
# HuggingFace models — use comfy-gen download url
comfy-gen download url https://huggingface.co/Comfy-Org/.../vae.safetensors --dest vae
comfy-gen download url https://huggingface.co/Comfy-Org/.../text_encoder.safetensors --dest text_encoders
comfy-gen download url https://huggingface.co/Comfy-Org/.../diffusion_model.safetensors --dest diffusion_models

# CivitAI models — use comfy-gen download civitai
comfy-gen download civitai 456789 --dest loras
```

For multiple models, you can run downloads sequentially or create a batch file:

```bash
cat > /tmp/downloads.json << 'EOF'
[
  {"source": "url", "url": "https://huggingface.co/.../vae.safetensors", "dest": "vae"},
  {"source": "url", "url": "https://huggingface.co/.../encoder.safetensors", "dest": "text_encoders"},
  {"source": "url", "url": "https://huggingface.co/.../model.safetensors", "dest": "diffusion_models"},
  {"source": "civitai", "version_id": "456789", "dest": "loras"}
]
EOF
comfy-gen download --batch /tmp/downloads.json
```

**Important**: Each `comfy-gen download` call submits a serverless job. The worker spins up, downloads to the volume, and shuts down. Large models (5-20GB) may take a few minutes. The 10-minute execution timeout applies — for very large models, the user may need to increase `executionTimeoutMs` on their endpoint via RunPod dashboard.

### Step 5: Submit the workflow

Once all models are confirmed on the volume, proceed to submission.

### Alternative: Pre-populated Templates

For common model families (Wan 2.2, Wan Animate), users can also deploy a [HearmemanAI RunPod template](https://get.runpod.io/wan-template) that downloads entire model sets at once. This is faster for initial setup:

1. Deploy the Wan template as a GPU pod attached to the same network volume
2. Set env vars: `download_wan22=true`, `download_wan_animate=true`
3. Wait for full deploy, then stop the pod — models persist on the volume

Mention this option when the workflow uses Wan models.

---

## Submit a Workflow

```bash
# Basic submission
comfy-gen submit workflow.json

# With input file mapped to a LoadImage node
comfy-gen submit workflow.json --input 193=/path/to/photo.jpg

# With parameter overrides
comfy-gen submit workflow.json --override 7.seed=42 --override 7.denoise=0.8

# Multiple inputs and overrides
comfy-gen submit workflow.json \
  --input 193=/path/to/ref.jpg \
  --input 417=/path/to/video.mp4 \
  --override 7.seed=12345 \
  --override 7.steps=20 \
  --timeout 300
```

**Progress output (stderr):**
```
Submitting to RunPod serverless endpoint...
Job submitted: abc-123-def
[3s]  IN_QUEUE
[6s]  node_check: Checking custom nodes (10%)
[9s]  inference: (2/8) KSampler Step 1/8 (29%)
[12s] inference: (5/8) KSampler Step 4/8 (55%)
[15s] upload: Uploading outputs to S3 (92%)
Completed in 27s (+2s queue). 1 image
```

**Result output (stdout):**
```json
{
  "ok": true,
  "output": {
    "url": "https://bucket.s3.region.amazonaws.com/comfy-gen/outputs/abc123.png",
    "seed": 1027836870258818,
    "resolution": {"width": 828, "height": 1248},
    "model_hashes": {
      "model.safetensors": {"sha256": "abc123...", "type": "diffusion_models"},
      "lora.safetensors": {"sha256": "def456...", "type": "loras", "strength": 0.8}
    }
  },
  "job_id": "21ff909b-...-e1",
  "delay_seconds": 2,
  "elapsed_seconds": 27
}
```

Key points:
- `--input NODE_ID=FILE_PATH` — maps a local file to a workflow node. Repeatable.
- `--override NODE_ID.PARAM=VALUE` — overrides any workflow parameter. Repeatable. Numbers auto-coerce.
- `--timeout SECONDS` — max wait (default: 600). Use 900+ for video workflows.
- `LoadImage` nodes with local file paths are auto-detected and uploaded even without `--input`.
- Output URLs are direct, permanent S3 links (no expiry).
- Output files have all metadata stripped (no embedded ComfyUI workflow data).
- `model_hashes` includes SHA256, type (parent folder name), and strength for LoRAs.

---

## Download Models

Download model files to the RunPod network volume via a serverless job. Files land directly at `/runpod-volume/ComfyUI/models/<dest>/`.

```bash
# Download a LoRA from CivitAI (use model VERSION ID, not model ID)
comfy-gen download civitai 456789 --dest loras

# Download a checkpoint from HuggingFace
comfy-gen download url https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors --dest checkpoints

# Download with a custom filename
comfy-gen download url https://example.com/model.safetensors --dest diffusion_models --filename my_model.safetensors

# Batch download from a JSON file
comfy-gen download --batch downloads.json
```

**Supported `--dest` values:** `checkpoints`, `loras`, `vae`, `clip`, `diffusion_models`, `text_encoders`, `controlnet`, `upscale_models`

**CivitAI downloads** require a token: `comfy-gen config --set civitai_token=<token>` (get it at https://civitai.com/user/account)

**CivitAI version ID**: Found on the CivitAI model page URL. It's the version-specific ID, NOT the top-level model ID.

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

---

## Check Job Status

```bash
comfy-gen status <job-id>
```

Returns: `{"job_id": "...", "status": "completed", "ok": true, "output": {...}, "elapsed_seconds": 66}`

Possible statuses: `in_queue`, `in_progress`, `completed`, `failed`, `cancelled`

## Cancel a Job

```bash
comfy-gen cancel <job-id>
```

---

## Configuration

```bash
comfy-gen config                              # Show all config
comfy-gen config --set runpod_api_key=rpa_... # Set a value
comfy-gen config --get endpoint_id            # Get a value
```

### Config keys

| Key | Env Var | Description |
|-----|---------|-------------|
| `runpod_api_key` | `RUNPOD_API_KEY` | RunPod API key |
| `endpoint_id` | `RUNPOD_ENDPOINT_ID` | Serverless endpoint ID |
| `aws_access_key_id` | `AWS_ACCESS_KEY_ID` | S3 access key |
| `aws_secret_access_key` | `AWS_SECRET_ACCESS_KEY` | S3 secret key |
| `s3_bucket` | `S3_BUCKET` | S3 bucket name |
| `s3_region` | `S3_REGION` | S3 region (default: eu-west-2) |
| `s3_endpoint_url` | `S3_ENDPOINT_URL` | Custom S3 endpoint (for R2/B2/MinIO) |
| `civitai_token` | `CIVITAI_TOKEN` | CivitAI API token (for `comfy-gen download civitai`) |
| `timeout_seconds` | `COMFY_GEN_TIMEOUT` | Max wait for completion (default: 600) |
| `poll_interval_seconds` | `COMFY_GEN_POLL_INTERVAL` | Status poll interval (default: 3) |

**Priority order:** config.json > .env file > env vars > defaults

Storage supports: AWS S3, Cloudflare R2, Backblaze B2, MinIO, DigitalOcean Spaces. For non-AWS providers, set `s3_endpoint_url`.

---

## Error Handling

- **Job failed**: Check the error message — common causes: missing models on volume, missing custom nodes (auto-installer usually handles this), S3 upload issues
- **Timeout**: Increase `--timeout`. Video workflows can take 300-900s.
- **IN_QUEUE for a long time**: Workers are cold-starting (~12s with FlashBoot, ~60s without). Normal for first job.
- **Missing models**: Use `comfy-gen download` to add models to the network volume before running workflows. See [Model Resolution](#step-2-search-for-models-on-huggingface) above.
- **Wrong workflow format**: Must be ComfyUI API format (node-ID-keyed JSON with `class_type`), not UI format.

### Retry strategy
If a transient error occurs (network, cold start), retry once after a short delay before reporting failure.

---

## Important Notes

- All stdout is JSON — parse it programmatically. Human logs go to stderr.
- The submit command blocks until completion or timeout.
- Workflow must be **ComfyUI API format** — export via "Save (API Format)" in ComfyUI UI.
- The serverless worker auto-installs missing custom nodes from the workflow.
- Output S3 URLs are permanent direct links — no authentication or expiry needed.
- **Security**: Never commit, display, or log API keys found in workflow JSONs (e.g., OpenRouter keys).
- For video workflows, always increase timeout (`--timeout 900` or more).
- Models must be on the network volume. Use `comfy-gen download` to populate it — or use the [model resolution workflow](#workflow-analysis--model-resolution--always-do-this-first) to find and download everything automatically.
