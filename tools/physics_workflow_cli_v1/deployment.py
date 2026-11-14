"""Locate the read-only shared CLI core and Physics Nodes domain package without mutating tools/blenderctl."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
import sys


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def core_directory() -> Path:
    configured = os.environ.get("PHYSICS_WORKFLOW_CLI_CORE_ROOT")
    path = Path(configured) if configured else project_root() / "tools" / "blenderctl"
    path = path.resolve(strict=True)
    if not all((path / name).is_file() for name in ("filesystem.py", "processes.py")):
        raise RuntimeError(f"Shared core missing filesystem.py/processes.py: {path}")
    return path


def addon_directory() -> Path:
    configured = os.environ.get("PHYSICS_WORKFLOW_ADDON_DIR")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    # Production checkout: <root>/tools/physics_workflow_cli_v1 and
    # <root>/tools/physics_nodes_workflow_addon are siblings.  The audit copy
    # keeps the same sibling relationship directly below shard-cli.
    candidates.append(Path(__file__).resolve().parent.parent / "physics_nodes_workflow_addon")
    candidates.append(project_root() / "tools" / "physics_nodes_workflow_addon")
    for path in candidates:
        path = path.resolve(strict=False)
        if path.is_dir() and (path / "__init__.py").is_file():
            return path
    raise RuntimeError("Physics Nodes addon directory missing; checked: " + ", ".join(str(p) for p in candidates))


def default_blender_binary(require_exists: bool = True) -> Path:
    env_bin = os.environ.get("BLENDER_PATH") or os.environ.get("BLENDERCTL_BLENDER")
    if env_bin:
        p = Path(env_bin)
        if not require_exists or p.is_file():
            return p
    relative = Path("runtime") / "blender" / "releases" / "5.2.1" / "blender-5.2.1-windows-x64" / "blender.exe"
    candidates = [project_root() / relative]
    # Keep the audit overlay self-locating without changing production layout.
    for ancestor in list(project_root().parents)[:5]:
        candidate = ancestor / relative
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    on_path = shutil.which("blender")
    if on_path:
        return Path(on_path)
    if not require_exists:
        return Path(env_bin) if env_bin else Path("blender")
    raise RuntimeError(
        "Default Blender 5.2.1 binary not found; checked: "
        + ", ".join(str(p) for p in candidates)
        + " (set BLENDER_PATH or BLENDERCTL_BLENDER)"
    )


def import_addon_module(submodule: str | None = None):
    tools_dir = str(addon_directory().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    name = "physics_nodes_workflow_addon" + (f".{submodule}" if submodule else "")
    return importlib.import_module(name)
