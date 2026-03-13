"""Storage layer for uploading input files to S3-compatible storage.

The worker downloads inputs via plain HTTP URLs, so any S3-compatible
provider that returns a pre-signed URL works.

Supported providers:
  AWS S3, Cloudflare R2, Backblaze B2, DigitalOcean Spaces, MinIO, etc.

Config keys (set via comfy-gen config --set):
  s3_endpoint_url         Custom endpoint (required for R2, B2, etc.)
  s3_region               AWS region (default: eu-west-2)
  s3_bucket               Bucket name
  aws_access_key_id       Access key
  aws_secret_access_key   Secret key

Examples:
  # AWS S3
  comfy-gen config --set aws_access_key_id=AKIA...
  comfy-gen config --set aws_secret_access_key=...
  comfy-gen config --set s3_bucket=my-bucket

  # Cloudflare R2
  comfy-gen config --set s3_endpoint_url=https://<account_id>.r2.cloudflarestorage.com
  comfy-gen config --set aws_access_key_id=...
  comfy-gen config --set aws_secret_access_key=...
  comfy-gen config --set s3_bucket=my-r2-bucket
"""

import hashlib
from pathlib import Path
from typing import Any


def upload_input(local_path: str, config: dict[str, Any] | None = None) -> str:
    """Upload a local file to S3 and return a pre-signed URL.

    Args:
        local_path: Path to the local file.
        config: Pre-loaded config dict. If None, loads from disk.

    Returns:
        A pre-signed S3 URL that the worker can GET.
    """
    if config is None:
        from comfy_gen.config import load
        config = load()

    return _upload_s3(local_path, config)


def _upload_s3(local_path: str, config: dict[str, Any]) -> str:
    """Upload to S3-compatible storage and return a pre-signed URL."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise RuntimeError(
            "boto3 is required for S3 storage. Install via: pip install boto3"
        )

    access_key = config.get("aws_access_key_id", "")
    secret_key = config.get("aws_secret_access_key", "")
    if not access_key or not secret_key:
        raise ValueError(
            "S3 credentials not configured. Set via:\n"
            "  comfy-gen config --set aws_access_key_id=AKIA...\n"
            "  comfy-gen config --set aws_secret_access_key=..."
        )

    region = config.get("s3_region", "eu-west-2")
    bucket = config.get("s3_bucket", "")
    endpoint_url = config.get("s3_endpoint_url", "")
    if not bucket:
        raise ValueError(
            "S3 bucket not configured. Set via:\n"
            "  comfy-gen config --set s3_bucket=my-bucket"
        )

    client_kwargs = {
        "region_name": region,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "config": Config(signature_version="s3v4"),
    }
    if endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url

    client = boto3.client("s3", **client_kwargs)

    # Content-addressed key to skip re-uploads
    ext = Path(local_path).suffix
    file_hash = hashlib.md5(Path(local_path).read_bytes()).hexdigest()[:12]
    key = f"comfy-gen/inputs/{file_hash}{ext}"

    try:
        client.head_object(Bucket=bucket, Key=key)
    except client.exceptions.ClientError:
        client.upload_file(local_path, bucket, key)

    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=3600,
    )
    return url
