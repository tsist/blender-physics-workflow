"""Explicit bridges from Geometry Nodes Physics Workbench to Blender native physics systems."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import bpy


def _builtin_dynamics_asset_blend() -> Path:
    datafiles = Path(bpy.utils.resource_path("LOCAL")) / "datafiles" / "assets" / "nodes" / "geometry_nodes_dynamics_assets.blend"
    return datafiles


def apply_bridge(
    obj: bpy.types.Object,
    request: dict[str, Any],
    collider_obj: bpy.types.Object | None = None,
) -> dict[str, Any]:
    mode = request["bridge_mode"]
    domain = request["domain"]
    params = request["parameters"]

    if mode == "GN_NATIVE":
        return {"bridge_mode": mode, "applied": False, "details": "Pure Geometry Nodes modular kernel active"}

    raise ValueError(f"UNSUPPORTED_BRIDGE: {mode} is reserved; a complete isolated setup, bake and verification adapter is not implemented")
