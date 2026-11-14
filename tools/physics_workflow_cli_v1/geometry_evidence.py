"""CLI adapter for the plugin-owned canonical geometry evidence sampler.

The implementation lives in ``physics_nodes_workflow_addon.geometry_evidence``
so the addon stays independently installable and production inspection uses
the exact same digest algorithm as the cold-reopen verifier.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


_CONFIGURED_ADDON = os.environ.get("PHYSICS_WORKFLOW_ADDON_DIR")
_ADDON_PARENT = (
    Path(_CONFIGURED_ADDON).resolve().parent
    if _CONFIGURED_ADDON
    else Path(__file__).resolve().parent.parent
)
if str(_ADDON_PARENT) not in sys.path:
    sys.path.insert(0, str(_ADDON_PARENT))

from physics_nodes_workflow_addon.geometry_evidence import (  # noqa: E402
    COUNT_FIELDS,
    DIGEST_FIELDS,
    EVIDENCE_SCHEMA,
    GeometryEvidenceError,
    QUANTIZATION_DECIMALS,
    REQUIRED_SUMMARY_FIELDS,
    sample_frame,
    sample_frames,
)

__all__ = [
    "COUNT_FIELDS",
    "DIGEST_FIELDS",
    "EVIDENCE_SCHEMA",
    "GeometryEvidenceError",
    "QUANTIZATION_DECIMALS",
    "REQUIRED_SUMMARY_FIELDS",
    "sample_frame",
    "sample_frames",
]
