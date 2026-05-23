"""Tests for the pure workflow validator.

`validate(workflow, object_info)` returns a list of human-readable failure
strings; empty list means the workflow passes pre-flight. Used by the smoke
gate to fail fast before submitting a workflow that won't pass ComfyUI's
own validation at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add automation/ to path so we can import validate_workflow.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "automation"))

from validate_workflow import validate  # noqa: E402


# Realistic object_info slice — covers an enum field, a typed primitive,
# and a connection-typed input.
OBJECT_INFO = {
    "KSampler": {
        "input": {
            "required": {
                "seed": ["INT", {"default": 0}],
                "sampler_name": [["euler", "dpmpp_2m"], {"default": "euler"}],
                "model": ["MODEL"],
                "latent_image": ["LATENT"],
            },
        },
    },
    "OnnxDetectionModelLoader": {
        "input": {
            "required": {
                "vitpose_model": [["vitpose_h_wholebody_data.bin"]],
                "yolo_model": [["vitpose_h_wholebody_data.bin"]],
            },
        },
    },
    "VAEDecode": {
        "input": {
            "required": {"samples": ["LATENT"], "vae": ["VAE"]},
        },
    },
}


def test_clean_workflow_passes():
    workflow = {
        "1": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                "sampler_name": "euler",
                "model": ["10", 0],
                "latent_image": ["20", 0],
            },
        },
    }
    assert validate(workflow, OBJECT_INFO) == []


def test_missing_class_detected():
    workflow = {
        "5": {"class_type": "NonexistentNode", "inputs": {}},
    }
    failures = validate(workflow, OBJECT_INFO)
    assert failures == ["Node 5 (NonexistentNode): class not installed"]


def test_missing_required_input_detected():
    workflow = {
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "sampler_name": "euler",
                "model": ["10", 0],
                "latent_image": ["20", 0],
            },
        },
    }
    failures = validate(workflow, OBJECT_INFO)
    assert failures == ["Node 7 (KSampler): missing required input 'seed'"]


def test_bad_enum_value_detected():
    workflow = {
        "9": {
            "class_type": "OnnxDetectionModelLoader",
            "inputs": {
                "vitpose_model": "vitpose_h_wholebody_data.bin",
                "yolo_model": "yolov10m.onnx",
            },
        },
    }
    failures = validate(workflow, OBJECT_INFO)
    assert len(failures) == 1
    assert "Node 9 (OnnxDetectionModelLoader)" in failures[0]
    assert "yolov10m.onnx" in failures[0]
    assert "yolo_model" in failures[0]


def test_connection_typed_input_skipped_from_checks():
    """[node_id, output_idx] connections satisfy 'provided' AND skip enum check."""
    workflow = {
        "1": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                # sampler_name is an enum, but the workflow supplies a connection.
                # Should be treated as runtime-resolved and skipped.
                "sampler_name": ["99", 0],
                "model": ["10", 0],
                "latent_image": ["20", 0],
            },
        },
    }
    assert validate(workflow, OBJECT_INFO) == []


def test_multiple_failures_all_reported():
    workflow = {
        "1": {"class_type": "GhostNode", "inputs": {}},
        "2": {
            "class_type": "OnnxDetectionModelLoader",
            "inputs": {
                "vitpose_model": "missing.onnx",
                # yolo_model also missing entirely
            },
        },
    }
    failures = validate(workflow, OBJECT_INFO)
    # 1 missing-class + 1 missing-required + 1 bad-enum = 3 failures
    assert len(failures) == 3
    joined = "\n".join(failures)
    assert "GhostNode" in joined
    assert "missing required input 'yolo_model'" in joined
    assert "missing.onnx" in joined
