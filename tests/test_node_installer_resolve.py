"""Tests for node_installer's class_type → repo resolution.

Focus: a node registered AFTER the last image build is absent from the
image-baked ComfyUI-Manager DB but present in the live GitHub
extension-node-map. It must still resolve (regression: it used to error with
"auto-installer could not find or install this node").
"""

from __future__ import annotations

from unittest.mock import patch

import node_installer


WORKFLOW = {
    "1": {"class_type": "KSampler"},
    "2": {"class_type": "Krea2EditModelPatch"},  # newer node, not in baked Manager DB
}

# Manager mappings baked into the image — knows KSampler, not the Krea2 node.
STALE_MANAGER_MAPPINGS = {
    "https://github.com/comfyanonymous/ComfyUI": [["KSampler"], {}],
}

# Live GitHub map — fetched fresh, includes the newer node.
LIVE_NODE_MAP = {
    "https://github.com/comfyanonymous/ComfyUI": [["KSampler"], {}],
    "https://github.com/lbouaraba/comfyui-krea2edit": [
        ["Krea2EditModelPatch", "Krea2EditGroundedEncode"],
        {},
    ],
}


def _run_ensure(mappings, installed_types):
    """Drive ensure_nodes with resolution mocked, stopping before any clone."""
    with patch.object(node_installer, "get_installed_node_types", return_value=installed_types), \
         patch.object(node_installer, "_get_manager_mappings", return_value=mappings), \
         patch.object(node_installer, "_get_manager_pack_stars", return_value={}), \
         patch.object(node_installer, "_resolve_repo_url", side_effect=lambda r, m: r), \
         patch.object(node_installer, "_get_fallback_node_map", return_value=LIVE_NODE_MAP), \
         patch.object(node_installer, "install_repo", return_value=True) as install, \
         patch.object(node_installer, "restart_comfyui", return_value=True):
        installed = node_installer.ensure_nodes(WORKFLOW)
    cloned = {c.args[0] for c in install.call_args_list}
    return installed, cloned


def test_newer_node_resolves_via_live_map_when_manager_stale():
    # Manager knows only KSampler (already installed); the Krea2 node is missing
    # from Manager but present in the live map — it must still get cloned.
    _, cloned = _run_ensure(STALE_MANAGER_MAPPINGS, {"KSampler"})
    assert "https://github.com/lbouaraba/comfyui-krea2edit" in cloned


def test_manager_wins_when_it_can_resolve():
    # If Manager already resolves the node, we don't need the live-map path.
    fresh = {
        "https://github.com/comfyanonymous/ComfyUI": [["KSampler"], {}],
        "https://github.com/lbouaraba/comfyui-krea2edit": [["Krea2EditModelPatch"], {}],
    }
    _, cloned = _run_ensure(fresh, {"KSampler"})
    assert cloned == {"https://github.com/lbouaraba/comfyui-krea2edit"}


def test_nothing_missing_short_circuits():
    with patch.object(node_installer, "get_installed_node_types",
                      return_value={"KSampler", "Krea2EditModelPatch"}):
        assert node_installer.ensure_nodes(WORKFLOW) == []
