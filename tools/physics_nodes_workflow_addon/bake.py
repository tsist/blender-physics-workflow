"""Managed disk bake lifecycle, ownership, atomic commit and cache integrity.

The original implementation deleted any path passed as ``bake_directory`` and
trusted a small manifest.  This module keeps the public function signatures but
changes the lifecycle to:

* keep every cache inside a random, marker-verified owner directory,
* write each bake into a fresh generation directory,
* hash and validate every meta/blob file,
* commit the generation manifest atomically,
* switch the active pointer only after full verification,
* never recursively delete an arbitrary user path (invalidation detaches the
  modifier reference and keeps the old directory).

``identity.py`` contains the layout-independent graph/dependency identity.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any
from datetime import datetime, timezone

import bpy

from . import identity as pnw_identity


MANIFEST_FILENAME = "physics-cache-manifest.json"
OWNER_MARKER_FILENAME = "physics-cache-owner.json"
CURRENT_FILENAME = "physics-cache-current.json"
FAILED_FILENAME = "physics-cache-failed.json"
INVALIDATED_FILENAME = "physics-cache-invalidated.json"
OWNED_DIR_NAME = "_pnw_owned"
GENERATIONS_DIR_NAME = "generations"
CACHE_SCHEMA_VERSION = "2.0"
OWNER_MARKER_SCHEMA_VERSION = "2.0"
CURRENT_SCHEMA_VERSION = "2.0"
WORKFLOW_BINDING_SCHEMA = "pnw-workflow-owner/1"
WORKFLOW_BINDING_KEY_PREFIX = "pnw_cache_owner_binding__"
MAX_WIN_PARENT_DIR = 240  # Kept for backwards-compatible imports
MAX_WIN_FILE_PATH = 259
GENERATION_TOKEN_BYTES = 9

_META_NAME_RE = re.compile(r"^(\d+)_(\d+)\.json$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Re-exported for the main thread's tree_fingerprint replacement.
semantic_tree_fingerprint = pnw_identity.semantic_tree_fingerprint
semantic_tree_payload = pnw_identity.semantic_tree_payload
CacheIdentityError = pnw_identity.CacheIdentityError


class CacheError(RuntimeError):
    """Raised when an owned cache generation cannot be created or verified."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        # Blender stores paths relative to the .blend as "//..."; after a
        # reopen that is not a filesystem absolute path.  Resolve it before
        # abspath so no UNC/root path is synthesized.
        if text.startswith("//"):
            text = bpy.path.abspath(text)
        expanded = os.path.expanduser(text)
        return Path(os.path.abspath(expanded))
    except (OSError, ValueError):
        return Path(text)


def _safe_exists(path: Path) -> bool:
    try:
        return Path(path).exists()
    except (OSError, ValueError):
        return False


def _safe_is_dir(path: Path) -> bool:
    try:
        return Path(path).is_dir()
    except (OSError, ValueError):
        return False


def _path_key(path: Path) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except Exception:
        return str(path)


def _safe_rel_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("\\", "/")
    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive:
        return None
    parts = candidate.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return candidate


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return pnw_identity.canonical_json(value)


def _read_json_file(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CacheError("cache_file_unreadable", f"Cannot read cache file: {path}", details={"error": str(exc)}) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CacheError("cache_file_encoding_invalid", f"Cache file is not valid UTF-8: {path}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CacheError("cache_file_json_invalid", f"Cache file is not valid JSON: {path}", details={"error": str(exc)}) from exc


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _atomic_write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


def _last_root_key(mod: bpy.types.Modifier) -> str:
    return f"pnw_cache_last_root__{getattr(mod, 'name', 'PhysicsNodes')}"


def _read_last_root(obj: bpy.types.Object, mod: bpy.types.Modifier) -> Path | None:
    try:
        value = obj.get(_last_root_key(mod))
    except Exception:
        return None
    return _path(value) if value else None


def _store_last_root(obj: bpy.types.Object, mod: bpy.types.Modifier, root: Path | None) -> None:
    key = _last_root_key(mod)
    try:
        if root is None:
            if key in obj:
                del obj[key]
        else:
            obj[key] = str(root)
    except Exception:
        pass


def _workflow_binding_key(mod: bpy.types.Modifier) -> str:
    return f"{WORKFLOW_BINDING_KEY_PREFIX}{getattr(mod, 'name', 'PhysicsNodes')}"


def _canonical_blend_path() -> str:
    filepath = str(getattr(bpy.data, "filepath", "") or "")
    if not filepath:
        return ""
    try:
        return os.path.normcase(os.path.abspath(bpy.path.abspath(filepath)))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(filepath))


def _workflow_binding_state(obj: bpy.types.Object, mod: bpy.types.Modifier) -> dict[str, Any]:
    """State that distinguishes *this* object/modifier workflow from same-named copies."""
    return {
        "schema": WORKFLOW_BINDING_SCHEMA,
        "source_blend_path": _canonical_blend_path(),
        "object_name": getattr(obj, "name", ""),
        "object_type": getattr(obj, "type", ""),
        "modifier_name": getattr(mod, "name", ""),
        # Blender's modifier persistent UID identifies a modifier within its
        # stack.  It is state, not a per-object UUID (it is shared by shallow
        # copies), so the owner token also gets its own generated local graph
        # UUID below.
        "modifier_persistent_uid": int(getattr(mod, "persistent_uid", 0) or 0),
    }


_WORKFLOW_STATE_FIELDS = (
    "schema",
    "source_blend_path",
    "object_name",
    "object_type",
    "modifier_name",
    "modifier_persistent_uid",
)

_WORKFLOW_BINDING_FIELDS = (
    *_WORKFLOW_STATE_FIELDS,
    "local_graph_uuid",
)


def _state_fields(binding: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(binding, dict):
        return {}
    return {field: binding.get(field) for field in _WORKFLOW_STATE_FIELDS}


def _binding_fields(binding: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(binding, dict):
        return {}
    return {field: binding.get(field) for field in _WORKFLOW_BINDING_FIELDS}


def _valid_workflow_token(token: Any) -> bool:
    return isinstance(token, str) and len(token) >= 16 and bool(re.fullmatch(r"[0-9a-fA-F]+", token))


def _read_workflow_binding(obj: bpy.types.Object, mod: bpy.types.Modifier) -> dict[str, Any] | None:
    try:
        raw = obj.get(_workflow_binding_key(mod))
    except Exception:
        return None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        binding = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(binding, dict):
        return None
    if binding.get("schema") != WORKFLOW_BINDING_SCHEMA:
        return None
    if not _valid_workflow_token(binding.get("workflow_owner_token")):
        return None
    if not _valid_workflow_token(binding.get("local_graph_uuid")):
        return None
    return binding


def _binding_matches(stored: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if not isinstance(stored, dict):
        return False
    stored_fields = _state_fields(stored)
    current_fields = _state_fields(current)
    if stored_fields.get("source_blend_path") == "":
        # A bake can happen before the first save.  Treat the stored empty path
        # as an origin wildcard; the persistent token still separates
        # independently created files, and the path is refreshed on the next
        # bake after save.
        stored_fields = dict(stored_fields)
        current_fields = dict(current_fields)
        stored_fields.pop("source_blend_path", None)
        current_fields.pop("source_blend_path", None)
    return stored_fields == current_fields


def _current_workflow_binding(obj: bpy.types.Object, mod: bpy.types.Modifier) -> dict[str, Any] | None:
    stored = _read_workflow_binding(obj, mod)
    if stored is None:
        return None
    current = _workflow_binding_state(obj, mod)
    return stored if _binding_matches(stored, current) else None


def _consumption_workflow_binding(obj: bpy.types.Object, mod: bpy.types.Modifier) -> dict[str, Any] | None:
    """Read-only binding for an explicit saved generation after Save-As.

    Save-As preserves the object custom binding/token but changes
    ``bpy.data.filepath``.  A read-only consumer may keep using the original
    persistent token/local graph UUID, provided object/modifier semantics still
    match exactly.  Only the source path is ignored here; writes use
    ``_current_workflow_binding`` and therefore create a new owner after
    Save-As.
    """
    stored = _read_workflow_binding(obj, mod)
    if stored is None:
        return None
    current = _workflow_binding_state(obj, mod)
    stored_fields = _state_fields(stored)
    current_fields = _state_fields(current)
    stored_fields.pop("source_blend_path", None)
    current_fields.pop("source_blend_path", None)
    return stored if stored_fields == current_fields else None


def _write_workflow_binding(obj: bpy.types.Object, mod: bpy.types.Modifier, binding: dict[str, Any]) -> None:
    obj[_workflow_binding_key(mod)] = json.dumps(binding, sort_keys=True, ensure_ascii=False)


def _ensure_workflow_binding(obj: bpy.types.Object, mod: bpy.types.Modifier) -> dict[str, Any]:
    """Persist/reuse a workflow token bound to the current object/modifier state."""
    current = _workflow_binding_state(obj, mod)
    stored = _read_workflow_binding(obj, mod)
    if stored is not None and _binding_matches(stored, current):
        if stored.get("source_blend_path") == "" and current.get("source_blend_path"):
            stored = dict(stored)
            stored["source_blend_path"] = current["source_blend_path"]
            _write_workflow_binding(obj, mod, stored)
        return stored
    binding = {
        **current,
        "workflow_owner_token": secrets.token_hex(GENERATION_TOKEN_BYTES),
        "local_graph_uuid": secrets.token_hex(16),
    }
    _write_workflow_binding(obj, mod, binding)
    return binding


def compute_input_mesh_sha256(obj: bpy.types.Object) -> str:
    """Stable digest of the base geometry/topology/attributes (excluding modifiers)."""
    return pnw_identity.base_geometry_sha256(obj)


def resolve_bake_directory(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    requested_dir: str | None = None,
) -> Path:
    """Resolve a cache candidate without creating or deleting anything."""
    candidate = _path(requested_dir)
    if candidate is None:
        candidate = _path(getattr(mod, "bake_directory", None))
    if candidate is None:
        candidate = _read_last_root(obj, mod)
    if candidate is None:
        if bpy.data.filepath:
            blend_path = _path(bpy.data.filepath) or Path(bpy.data.filepath)
            short_key = hashlib.sha1(f"{obj.name}:{mod.name}".encode("utf-8")).hexdigest()[:10]
            candidate = blend_path.parent / "pnw_bakes" / short_key
        else:
            short_key = hashlib.sha1(f"{obj.name}:{mod.name}".encode("utf-8")).hexdigest()[:10]
            candidate = Path(tempfile.gettempdir()) / "pnw_bakes" / short_key
    return candidate


def read_cache_manifest(bake_dir: Path) -> dict[str, Any] | None:
    """Read a manifest as a dict; invalid JSON/encoding/types return None."""
    path = Path(bake_dir) / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        payload = _read_json_file(path)
    except CacheError:
        return None
    return payload if isinstance(payload, dict) else None


def cache_stats(bake_dir: Path) -> dict[str, Any]:
    """Count *active* cache files without trusting or mutating the cache.

    Retained generations are intentionally kept after invalidation.  They are
    reported through ``retained_total_bytes`` but do not make the active cache
    look baked.
    """
    bake_dir = Path(bake_dir)
    result: dict[str, Any] = {
        "exists": _safe_exists(bake_dir),
        "meta_files": 0,
        "blob_files": 0,
        "total_bytes": 0,
        "retained_total_bytes": 0,
        "manifest_present": (bake_dir / MANIFEST_FILENAME).is_file(),
        "owner_marker_present": (bake_dir / OWNER_MARKER_FILENAME).is_file(),
        "current_present": (bake_dir / CURRENT_FILENAME).is_file(),
    }
    if not _safe_is_dir(bake_dir):
        return result

    def count_paths(root: Path) -> tuple[int, int, int]:
        try:
            metas = [
                path
                for path in root.rglob("*.json")
                if path.is_file() and path.parent.name == "meta" and path.name != MANIFEST_FILENAME
            ]
            blobs = [path for path in root.rglob("*.blob") if path.is_file() and path.parent.name == "blobs"]
        except OSError:
            return 0, 0, 0
        total = 0
        for path in metas + blobs:
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return len(metas), len(blobs), total

    active_dirs: list[Path] = []
    if (bake_dir / OWNER_MARKER_FILENAME).is_file() or (bake_dir / CURRENT_FILENAME).is_file():
        current = _read_current(bake_dir)
        if current and current.get("status") == "committed":
            generation = current.get("generation")
            if isinstance(generation, str) and _safe_rel_path(generation) is not None:
                candidate = bake_dir / GENERATIONS_DIR_NAME / generation
                if _safe_is_dir(candidate):
                    active_dirs.append(candidate)
    elif bake_dir.parent.name == GENERATIONS_DIR_NAME:
        active_dirs.append(bake_dir)
    elif (bake_dir / OWNED_DIR_NAME).is_dir():
        try:
            roots = [child for child in (bake_dir / OWNED_DIR_NAME).iterdir() if _safe_is_dir(child)]
        except OSError:
            roots = []
        best: tuple[str, Path] | None = None
        for root in roots:
            current = _read_current(root)
            if current and current.get("status") == "committed":
                generation = current.get("generation")
                if isinstance(generation, str) and _safe_rel_path(generation) is not None:
                    candidate = root / GENERATIONS_DIR_NAME / generation
                    if _safe_is_dir(candidate):
                        stamp = str(current.get("committed_utc", ""))
                        if best is None or stamp > best[0]:
                            best = (stamp, candidate)
        if best is not None:
            active_dirs.append(best[1])
    else:
        active_dirs.append(bake_dir)

    meta_count = 0
    blob_count = 0
    total = 0
    for root in active_dirs:
        metas, blobs, size = count_paths(root)
        meta_count += metas
        blob_count += blobs
        total += size

    _, _, retained_total = count_paths(bake_dir)
    result.update(
        {
            "meta_files": meta_count,
            "blob_files": blob_count,
            "total_bytes": total,
            "retained_total_bytes": retained_total,
        }
    )
    return result


# ---------------------------------------------------------------------------
# Owner marker and current pointer


def _owner_marker_payload(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    token: str,
    workflow_binding: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": OWNER_MARKER_SCHEMA_VERSION,
        "kind": "pnw_cache_owner",
        "owner_token": token,
        "workflow_owner_token": workflow_binding["workflow_owner_token"],
        "workflow_binding": _binding_fields(workflow_binding),
        "object_name": getattr(obj, "name", ""),
        "modifier_name": getattr(mod, "name", ""),
        "generator": "physics_nodes_workflow_addon.bake",
        "format": 2,
        "created_utc": _utc_now(),
    }


def _marker_checksum(payload: dict[str, Any]) -> str:
    data = dict(payload)
    data.pop("marker_sha256", None)
    return _sha256_text(_canonical_json(data))


def _write_owner_marker(
    root: Path,
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    token: str,
    workflow_binding: dict[str, Any],
) -> dict[str, Any]:
    payload = _owner_marker_payload(obj, mod, token, workflow_binding)
    payload["marker_sha256"] = _marker_checksum(payload)
    _atomic_write_json(Path(root) / OWNER_MARKER_FILENAME, payload)
    return payload


def _read_owner_marker(root: Path) -> dict[str, Any] | None:
    path = Path(root) / OWNER_MARKER_FILENAME
    if not path.is_file():
        return None
    try:
        payload = _read_json_file(path)
    except CacheError:
        return None
    return payload if isinstance(payload, dict) else None


def _verify_owner_marker(
    root: Path,
    obj: bpy.types.Object | None = None,
    mod: bpy.types.Modifier | None = None,
    expected_binding: dict[str, Any] | None = None,
    allow_source_mismatch: bool = False,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    payload = _read_owner_marker(root)
    if payload is None:
        return False, "missing", None
    if payload.get("kind") != "pnw_cache_owner":
        return False, "kind_mismatch", payload
    if str(payload.get("schema_version")) != OWNER_MARKER_SCHEMA_VERSION:
        return False, "schema_mismatch", payload
    token = payload.get("owner_token")
    if not isinstance(token, str) or len(token) < 16 or not re.fullmatch(r"[0-9a-fA-F]+", token):
        return False, "token_invalid", payload
    if payload.get("generator") != "physics_nodes_workflow_addon.bake":
        return False, "generator_mismatch", payload
    if not isinstance(payload.get("marker_sha256"), str):
        return False, "checksum_missing", payload
    if payload["marker_sha256"] != _marker_checksum(payload):
        return False, "checksum_mismatch", payload
    if Path(root).name != f"owner_{token}":
        return False, "directory_name_mismatch", payload
    if obj is not None and payload.get("object_name") != getattr(obj, "name", ""):
        return False, "workflow_owner_object_mismatch", payload
    if mod is not None and payload.get("modifier_name") != getattr(mod, "name", ""):
        return False, "workflow_owner_modifier_mismatch", payload
    if expected_binding is None and obj is not None and mod is not None:
        expected_binding = (
            _consumption_workflow_binding(obj, mod)
            if allow_source_mismatch
            else _current_workflow_binding(obj, mod)
        )
        if expected_binding is None:
            return False, "workflow_owner_missing", payload
    if expected_binding is not None:
        if not _valid_workflow_token(payload.get("workflow_owner_token")):
            return False, "workflow_owner_missing", payload
        if payload.get("workflow_owner_token") != expected_binding.get("workflow_owner_token"):
            return False, "workflow_owner_mismatch", payload
        marker_binding = payload.get("workflow_binding")
        if not isinstance(marker_binding, dict):
            return False, "workflow_owner_missing", payload
        marker_fields = _binding_fields(marker_binding)
        expected_fields = _binding_fields(expected_binding)
        if marker_fields.get("source_blend_path") == "":
            marker_fields = dict(marker_fields)
            expected_fields = dict(expected_fields)
            marker_fields.pop("source_blend_path", None)
            expected_fields.pop("source_blend_path", None)
        if marker_fields != expected_fields:
            return False, "workflow_owner_mismatch", payload
    return True, None, payload


def _current_checksum(payload: dict[str, Any]) -> str:
    data = dict(payload)
    data.pop("pointer_sha256", None)
    return _sha256_text(_canonical_json(data))


def _read_current(root: Path) -> dict[str, Any] | None:
    path = Path(root) / CURRENT_FILENAME
    if not path.is_file():
        return None
    try:
        payload = _read_json_file(path)
    except CacheError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != "pnw_cache_current":
        return None
    if str(payload.get("schema_version")) != CURRENT_SCHEMA_VERSION:
        return None
    checksum = payload.get("pointer_sha256")
    if not isinstance(checksum, str) or checksum != _current_checksum(payload):
        return None
    return payload


def _write_current(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    data["schema_version"] = CURRENT_SCHEMA_VERSION
    data["kind"] = "pnw_cache_current"
    data["pointer_sha256"] = _current_checksum(data)
    _atomic_write_json(Path(root) / CURRENT_FILENAME, data)
    return data


def _snapshot_current_bytes(root: Path) -> bytes | None:
    path = Path(root) / CURRENT_FILENAME
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore_current_bytes(root: Path, data: bytes | None) -> None:
    path = Path(root) / CURRENT_FILENAME
    if data is None:
        try:
            if path.is_file():
                os.unlink(path)
        except OSError:
            pass
        return
    try:
        _atomic_write_bytes(path, data)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Ownership discovery / classification


def _matching_owned_roots(
    base: Path,
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    *,
    readonly: bool = False,
) -> list[Path]:
    owned = Path(base) / OWNED_DIR_NAME
    if not owned.is_dir():
        return []
    binding = _consumption_workflow_binding(obj, mod) if readonly else _current_workflow_binding(obj, mod)
    if binding is None:
        return []
    roots: list[Path] = []
    try:
        children = sorted(owned.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        ok, _reason, _marker = _verify_owner_marker(
            child,
            obj,
            mod,
            expected_binding=binding,
        )
        if ok:
            roots.append(child)
    return roots


def _pick_root(roots: list[Path]) -> Path | None:
    if not roots:
        return None
    ranked: list[tuple[str, str, Path]] = []
    for root in roots:
        current = _read_current(root)
        status = str(current.get("status")) if current else ""
        timestamp = str(current.get("committed_utc") or current.get("invalidated_utc") or "") if current else ""
        if not timestamp:
            marker = _read_owner_marker(root)
            timestamp = str(marker.get("created_utc", "")) if marker else ""
        rank = "2" if status == "committed" else "1" if status == "invalidated" else "0"
        ranked.append((rank, timestamp, root))
    ranked.sort(key=lambda item: (item[0], item[1], item[2].name))
    return ranked[-1][2]


def _has_legacy_layout(candidate: Path) -> bool:
    try:
        for child in candidate.iterdir():
            if child.is_dir() and (child / "meta").is_dir() and (child / "blobs").is_dir():
                return True
    except OSError:
        return False
    return False


def _classify_candidate(
    candidate: Path,
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    *,
    readonly: bool = False,
) -> tuple[str, Path | None, str | None]:
    """Return (kind, owner_root, reason).  Never writes to disk."""
    candidate = Path(candidate)
    if not _safe_exists(candidate):
        return "missing", None, None
    if not _safe_is_dir(candidate):
        return "file", None, "cache_path_is_file"

    if candidate.parent.name == GENERATIONS_DIR_NAME:
        root = candidate.parent.parent
        ok, reason, _marker = _verify_owner_marker(
            root,
            obj,
            mod,
            allow_source_mismatch=readonly,
        )
        if not ok:
            return "generation", root, f"owner_marker_{reason}"
        if (candidate / MANIFEST_FILENAME).is_file():
            return "generation", root, None
        return "partial_generation", root, "cache_manifest_missing"

    ok, reason, marker = _verify_owner_marker(
        candidate,
        obj,
        mod,
        allow_source_mismatch=readonly,
    )
    if ok:
        return "root", candidate, None
    if marker is not None:
        return "root", candidate, f"owner_marker_{reason}"

    roots = _matching_owned_roots(candidate, obj, mod, readonly=readonly)
    if roots:
        return "root", _pick_root(roots), None

    # A directory that looks managed but whose marker is gone/moved must not be
    # silently adopted or nested into.
    if _safe_exists(candidate / GENERATIONS_DIR_NAME) or (candidate / CURRENT_FILENAME).is_file():
        return "managed_root", candidate, "owner_marker_missing"
    if candidate.name.startswith("owner_") and candidate.parent.name == OWNED_DIR_NAME:
        return "managed_root", candidate, "owner_marker_missing"

    if (candidate / MANIFEST_FILENAME).is_file():
        manifest = read_cache_manifest(candidate)
        if manifest and str(manifest.get("schema_version")) == CACHE_SCHEMA_VERSION:
            return "uncommitted_root", candidate, "cache_pointer_missing"
        return "legacy", candidate, "legacy_cache_manifest"

    if _has_legacy_layout(candidate):
        return "legacy", candidate, "legacy_cache_layout"

    return "foreign", None, None


def _check_generation_path_budget(gen_dir: Path) -> None:
    if os.name != "nt":
        return
    probe = Path(gen_dir) / "0000000000" / "meta" / "00001_00000.json"
    if len(str(probe)) > MAX_WIN_FILE_PATH:
        raise ValueError(
            f"Bake generation path exceeds Windows safe length budget "
            f"({len(str(probe))} > {MAX_WIN_FILE_PATH}): {probe}"
        )


def _owner_base_for_candidate(candidate: Path, root: Path | None) -> Path:
    """Derive the base that contains `_pnw_owned/` when an inherited root is foreign."""
    if root is not None:
        if root.parent.name == OWNED_DIR_NAME:
            return root.parent.parent
        return root
    if candidate.parent.name == OWNED_DIR_NAME:
        return candidate.parent.parent
    if candidate.name == OWNED_DIR_NAME:
        return candidate.parent
    return candidate


def _ensure_owner_root(
    candidate: Path,
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    workflow_binding: dict[str, Any],
) -> Path:
    kind, root, reason = _classify_candidate(candidate, obj, mod)
    base: Path | None = None
    if kind in {"root", "generation", "partial_generation"}:
        if reason and reason.startswith("workflow_owner_"):
            # A copied object inherits mod.bake_directory/custom properties.
            # Keep the foreign root untouched and allocate a fresh owned root.
            base = _owner_base_for_candidate(candidate, root)
        elif reason:
            raise CacheError("cache_owner_unverified", f"Cache owner cannot be verified: {reason}", details={"path": str(candidate)})
        elif root is None:
            raise CacheError("cache_owner_unverified", "Cache owner root is unknown")
        else:
            return root
    if kind == "file":
        raise CacheError("cache_path_is_file", f"Bake directory is a file: {candidate}")
    if kind in {"managed_root", "uncommitted_root"}:
        raise CacheError(
            "cache_owner_unverified",
            f"Managed cache directory is missing or has a damaged owner marker: {candidate}",
            details={"reason": reason},
        )

    # Generic/base/legacy/foreign directory: never delete it, never adopt it.
    # Create a fresh random owner child instead.
    if base is None:
        base = Path(candidate)
    if _safe_exists(base) and not _safe_is_dir(base):
        raise CacheError("cache_path_is_file", f"Bake directory is a file: {base}")
    base.mkdir(parents=True, exist_ok=True)
    owned = base if base.name == OWNED_DIR_NAME else base / OWNED_DIR_NAME
    owned.mkdir(parents=True, exist_ok=True)

    for _attempt in range(16):
        token = secrets.token_hex(GENERATION_TOKEN_BYTES)
        root = owned / f"owner_{token}"
        if _safe_exists(root):
            ok, marker_reason, marker = _verify_owner_marker(
                root,
                obj,
                mod,
                expected_binding=workflow_binding,
            )
            if ok:
                return root
            if marker is not None:
                raise CacheError(
                    "cache_owner_marker_invalid",
                    f"Owner marker exists but is invalid: {marker_reason}",
                    details={"path": str(root)},
                )
            # Random collision with an empty/partial directory: try another.
            continue
        try:
            root.mkdir()
        except FileExistsError:
            continue
        _write_owner_marker(root, obj, mod, token, workflow_binding)
        ok, marker_reason, _marker = _verify_owner_marker(
            root,
            obj,
            mod,
            expected_binding=workflow_binding,
        )
        if not ok:
            raise CacheError("cache_owner_marker_write_failed", f"Owner marker verification failed: {marker_reason}")
        return root
    raise CacheError("cache_owner_root_collision", f"Could not allocate an owner directory under {owned}")


def _create_generation_dir(root: Path) -> Path:
    generations = Path(root) / GENERATIONS_DIR_NAME
    generations.mkdir(parents=True, exist_ok=True)
    for _attempt in range(16):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        generation = generations / f"g{stamp}_{secrets.token_hex(5)}"
        _check_generation_path_budget(generation)
        try:
            generation.mkdir()
            return generation
        except FileExistsError:
            continue
    raise CacheError("cache_generation_collision", f"Could not allocate a generation directory under {generations}")


# ---------------------------------------------------------------------------
# Generation file scanning / manifest


def _collect_blob_refs(value: Any, out: list[tuple[str, int, int]]) -> None:
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str) and name.endswith(".blob"):
            if "start" not in value or "size" not in value:
                raise CacheError("meta_blob_reference_invalid", f"Blob reference without start/size: {name}")
            start = value.get("start")
            size = value.get("size")
            if not isinstance(start, int) or isinstance(start, bool) or not isinstance(size, int) or isinstance(size, bool):
                raise CacheError("meta_blob_reference_invalid", f"Blob reference has non-integer range: {name}")
            out.append((name, start, size))
        for item in value.values():
            _collect_blob_refs(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_blob_refs(item, out)


def _parse_meta(path: Path) -> tuple[int, list[tuple[str, int, int]]]:
    match = _META_NAME_RE.match(path.name)
    if match is None:
        raise CacheError("meta_filename_invalid", f"Cache meta filename does not contain a frame: {path.name}")
    frame = int(match.group(1))
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        raise CacheError("meta_schema_invalid", f"Cache meta is not a JSON object: {path}")
    if not isinstance(payload.get("version"), int):
        raise CacheError("meta_schema_invalid", f"Cache meta has no integer version: {path}")
    if not isinstance(payload.get("items"), dict):
        raise CacheError("meta_schema_invalid", f"Cache meta has no items object: {path}")
    refs: list[tuple[str, int, int]] = []
    _collect_blob_refs(payload, refs)
    return frame, refs


def _entry_for_file(path: Path, gen_dir: Path, kind: str, baked_id: int, frame: int | None = None) -> dict[str, Any]:
    if path.is_symlink():
        raise CacheError("cache_file_symlink", f"Cache files must not be symlinks: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CacheError("cache_file_unreadable", f"Cannot stat cache file: {path}", details={"error": str(exc)}) from exc
    return {
        "path": path.relative_to(gen_dir).as_posix(),
        "kind": kind,
        "baked_id": int(baked_id),
        "frame": int(frame) if frame is not None else None,
        "size": int(size),
        "sha256": _sha256_file(path),
    }


def _scan_generation(
    gen_dir: Path,
    baked_ids: list[int],
    frame_start: int,
    frame_end: int,
) -> dict[str, Any]:
    """Validate an on-disk generation and return its exact file inventory."""
    gen_dir = Path(gen_dir)
    if not gen_dir.is_dir():
        raise CacheError("cache_generation_missing", f"Bake generation directory is missing: {gen_dir}")
    if frame_end < frame_start:
        raise CacheError("invalid_frame_range", f"Invalid frame range: {frame_start}..{frame_end}")
    if not baked_ids:
        raise CacheError("bake_ids_missing", "No bake ids were reported by the modifier")
    unique_ids = sorted({int(value) for value in baked_ids})
    if len(unique_ids) != len(baked_ids):
        raise CacheError("bake_ids_duplicated", f"Duplicate bake ids: {baked_ids}")

    expected_frames = set(range(int(frame_start), int(frame_end) + 1))
    entries: list[dict[str, Any]] = []
    meta_count = 0
    blob_count = 0
    total_bytes = 0
    referenced_blobs: set[str] = set()

    try:
        child_dirs = sorted(path for path in gen_dir.iterdir() if path.is_dir())
    except OSError as exc:
        raise CacheError("cache_generation_unreadable", f"Cannot read generation: {gen_dir}") from exc
    for child in child_dirs:
        if not child.name.isdigit() or int(child.name) not in unique_ids:
            raise CacheError("cache_unlisted_bake_dir", f"Unexpected bake directory in generation: {child.name}")

    for bake_id in unique_ids:
        id_dir = gen_dir / str(bake_id)
        meta_dir = id_dir / "meta"
        blob_dir = id_dir / "blobs"
        if not meta_dir.is_dir() or not blob_dir.is_dir():
            raise CacheError(
                "bake_output_incomplete",
                f"Bake {bake_id} is missing meta/blobs directories: {id_dir}",
            )
        try:
            meta_paths = sorted(path for path in meta_dir.glob("*.json") if path.is_file())
            blob_paths = sorted(path for path in blob_dir.glob("*.blob") if path.is_file())
        except OSError as exc:
            raise CacheError("cache_generation_unreadable", f"Cannot read bake {bake_id} files") from exc
        if not meta_paths:
            raise CacheError("cache_meta_missing", f"Bake {bake_id} has no meta files")
        if not blob_paths:
            raise CacheError("cache_blob_missing", f"Bake {bake_id} has no blob files")

        blob_sizes: dict[str, int] = {}
        for blob in blob_paths:
            if blob.is_symlink():
                raise CacheError("cache_file_symlink", f"Cache files must not be symlinks: {blob}")
            try:
                blob_sizes[blob.name] = blob.stat().st_size
            except OSError as exc:
                raise CacheError("cache_file_unreadable", f"Cannot stat blob: {blob}") from exc

        frames: set[int] = set()
        for meta_path in meta_paths:
            frame, refs = _parse_meta(meta_path)
            frames.add(frame)
            for blob_name, start, size in refs:
                if Path(blob_name).name != blob_name or "/" in blob_name or "\\" in blob_name:
                    raise CacheError("meta_blob_reference_invalid", f"Blob reference escapes the bake directory: {blob_name}")
                if blob_name not in blob_sizes:
                    raise CacheError("cache_blob_missing", f"Meta references missing blob: {bake_id}/{blob_name}")
                if start < 0 or size < 0 or start + size > blob_sizes[blob_name]:
                    raise CacheError(
                        "cache_blob_range_invalid",
                        f"Meta reference exceeds blob size: {bake_id}/{blob_name} start={start} size={size} blob={blob_sizes[blob_name]}",
                    )
                referenced_blobs.add(f"{bake_id}/blobs/{blob_name}")
            entries.append(_entry_for_file(meta_path, gen_dir, "meta", bake_id, frame))
            meta_count += 1
            total_bytes += entries[-1]["size"]

        missing = expected_frames - frames
        extra = frames - expected_frames
        if missing:
            raise CacheError(
                "cache_frames_missing",
                f"Bake {bake_id} is missing frames {sorted(missing)[:8]} (expected {frame_start}..{frame_end})",
            )
        if extra:
            raise CacheError("cache_frames_extra", f"Bake {bake_id} contains frames outside range: {sorted(extra)[:8]}")

        for blob_path in blob_paths:
            entry = _entry_for_file(blob_path, gen_dir, "blob", bake_id)
            entries.append(entry)
            blob_count += 1
            total_bytes += entry["size"]

    entries.sort(key=lambda item: str(item["path"]))
    return {
        "entries": entries,
        "meta_entries": [entry for entry in entries if entry["kind"] == "meta"],
        "blob_entries": [entry for entry in entries if entry["kind"] == "blob"],
        "stats": {
            "meta_files": meta_count,
            "blob_files": blob_count,
            "total_bytes": total_bytes,
            "frame_start": int(frame_start),
            "frame_end": int(frame_end),
            "baked_ids": unique_ids,
            "referenced_blobs": sorted(referenced_blobs),
        },
    }


def _manifest_checksum(payload: dict[str, Any]) -> str:
    data = dict(payload)
    data.pop("manifest_sha256", None)
    return _sha256_text(_canonical_json(data))


def _manifest_payload(
    gen_dir: Path,
    owner_token: str,
    cache_identity: dict[str, Any],
    params_hash: str,
    graph_fingerprint: str,
    scan: dict[str, Any],
    frame_start: int,
    frame_end: int,
) -> dict[str, Any]:
    target = cache_identity.get("target") or {}
    modifier = cache_identity.get("modifier") or {}
    semantic_fp = ((modifier.get("node_group") or {}).get("semantic_fingerprint")) if isinstance(modifier, dict) else None
    payload: dict[str, Any] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": "pnw_cache_generation",
        "generation": Path(gen_dir).name,
        "owner_token": owner_token,
        "created_utc": _utc_now(),
        "object_name": target.get("name"),
        "modifier_name": modifier.get("name"),
        "frame_start": int(frame_start),
        "frame_end": int(frame_end),
        "baked_ids": list(scan["stats"]["baked_ids"]),
        "parameters_hash": str(params_hash or ""),
        "caller_graph_fingerprint": str(graph_fingerprint or ""),
        "semantic_graph_fingerprint": semantic_fp,
        "identity": cache_identity,
        "identity_hash": cache_identity.get("identity_hash"),
        # Backwards-compatible summary fields for older inspectors.
        "input_mesh_sha256": target.get("base_geometry_sha256"),
        "graph_fingerprint": str(graph_fingerprint or semantic_fp or ""),
        "meta_files": scan["stats"]["meta_files"],
        "blob_files": scan["stats"]["blob_files"],
        "total_bytes": scan["stats"]["total_bytes"],
        "files": scan["entries"],
        "stats": scan["stats"],
        "bake_target": "DISK",
    }
    payload["manifest_sha256"] = _manifest_checksum(payload)
    return payload


def _read_generation_manifest(gen_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = Path(gen_dir) / MANIFEST_FILENAME
    if not path.is_file():
        return None, "cache_manifest_missing"
    try:
        payload = _read_json_file(path)
    except CacheError as exc:
        return None, exc.code
    if not isinstance(payload, dict):
        return None, "cache_manifest_corrupt"
    if str(payload.get("schema_version")) != CACHE_SCHEMA_VERSION:
        return None, "unsupported_cache_schema"
    if payload.get("kind") != "pnw_cache_generation":
        return None, "cache_manifest_corrupt"
    checksum = payload.get("manifest_sha256")
    if not isinstance(checksum, str) or checksum != _manifest_checksum(payload):
        return None, "cache_manifest_corrupt"
    return payload, None


def _verify_generation_files(gen_dir: Path, manifest: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Verify manifest inventory, hashes, frame coverage and blob references."""
    try:
        baked_ids = manifest.get("baked_ids")
        if not isinstance(baked_ids, list) or not all(isinstance(value, int) and not isinstance(value, bool) for value in baked_ids):
            return False, "cache_manifest_corrupt", None
        frame_start = manifest.get("frame_start")
        frame_end = manifest.get("frame_end")
        if not isinstance(frame_start, int) or not isinstance(frame_end, int):
            return False, "cache_manifest_corrupt", None
        if manifest.get("generation") != Path(gen_dir).name:
            return False, "cache_generation_mismatch", None
        identity_payload = manifest.get("identity")
        if not isinstance(identity_payload, dict):
            return False, "cache_manifest_corrupt", None
        if manifest.get("identity_hash") != identity_payload.get("identity_hash"):
            return False, "cache_identity_hash_mismatch", None
        if not isinstance(manifest.get("files"), list):
            return False, "cache_manifest_corrupt", None

        scan = _scan_generation(gen_dir, list(baked_ids), frame_start, frame_end)
    except CacheError as exc:
        return False, exc.code, {"message": str(exc)}

    actual_meta = {entry["path"]: entry for entry in scan["meta_entries"]}
    actual_blobs = {entry["path"]: entry for entry in scan["blob_entries"]}
    manifest_meta: dict[str, dict[str, Any]] = {}
    manifest_blobs: dict[str, dict[str, Any]] = {}
    seen_paths: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            return False, "cache_manifest_corrupt", None
        relative = _safe_rel_path(entry.get("path"))
        if relative is None:
            return False, "cache_manifest_path_invalid", None
        rel_text = relative.as_posix()
        if rel_text in seen_paths:
            return False, "cache_manifest_duplicate_file", None
        seen_paths.add(rel_text)
        kind = entry.get("kind")
        if kind not in {"meta", "blob"}:
            return False, "cache_manifest_corrupt", None
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            return False, "cache_manifest_corrupt", None
        if not isinstance(entry.get("sha256"), str) or not _SHA256_RE.fullmatch(entry["sha256"]):
            return False, "cache_manifest_corrupt", None
        target_map = manifest_meta if kind == "meta" else manifest_blobs
        target_map[rel_text] = entry

    if set(manifest_meta) != set(actual_meta):
        missing = sorted(set(manifest_meta) - set(actual_meta))
        extra = sorted(set(actual_meta) - set(manifest_meta))
        return (
            False,
            "cache_meta_inventory_mismatch",
            {"missing": missing[:8], "extra": extra[:8]},
        )

    for rel_text, entry in manifest_blobs.items():
        actual = actual_blobs.get(rel_text)
        if actual is None:
            return False, "cache_file_missing", {"path": rel_text}
        if int(actual["size"]) != int(entry["size"]) or actual["sha256"] != entry["sha256"]:
            return False, "cache_file_corrupt", {"path": rel_text}

    # Every blob referenced by a meta file must be listed in the manifest.
    for rel_text in scan["stats"]["referenced_blobs"]:
        actual = actual_blobs.get(rel_text)
        if actual is None:
            return False, "cache_file_missing", {"path": rel_text}
        if rel_text not in manifest_blobs:
            return False, "cache_unlisted_blob_reference", {"path": rel_text}
        if int(actual["size"]) != int(manifest_blobs[rel_text]["size"]) or actual["sha256"] != manifest_blobs[rel_text]["sha256"]:
            return False, "cache_file_corrupt", {"path": rel_text}

    # Every meta file must be listed and must match size/hash.
    for rel_text, entry in manifest_meta.items():
        actual = actual_meta.get(rel_text)
        if actual is None:
            return False, "cache_file_missing", {"path": rel_text}
        if int(actual["size"]) != int(entry["size"]) or actual["sha256"] != entry["sha256"]:
            return False, "cache_file_corrupt", {"path": rel_text}

    stats = manifest.get("stats")
    if isinstance(stats, dict):
        for key in ("meta_files", "blob_files", "total_bytes"):
            if stats.get(key) != scan["stats"].get(key):
                return False, "cache_stats_mismatch", {"key": key}
        if stats.get("baked_ids") != scan["stats"].get("baked_ids"):
            return False, "cache_stats_mismatch", {"key": "baked_ids"}
    for key in ("meta_files", "blob_files", "total_bytes"):
        if key in manifest and manifest.get(key) != scan["stats"].get(key):
            return False, "cache_stats_mismatch", {"key": key}
    target_identity = identity_payload.get("target") if isinstance(identity_payload, dict) else None
    if isinstance(target_identity, dict) and "input_mesh_sha256" in manifest:
        if manifest.get("input_mesh_sha256") != target_identity.get("base_geometry_sha256"):
            return False, "cache_input_mesh_mismatch", None
    if manifest["identity"].get("frames") != {"start": frame_start, "end": frame_end}:
        return False, "cache_frame_identity_mismatch", None
    return True, None, scan


def _failure_marker(gen_dir: Path, exc: BaseException, identity_hash: str | None) -> None:
    try:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "kind": "pnw_cache_failed",
            "generation": Path(gen_dir).name,
            "created_utc": _utc_now(),
            "error_type": type(exc).__name__,
            "error_code": getattr(exc, "code", None),
            "message": str(exc),
            "identity_hash": identity_hash,
        }
        _atomic_write_json(Path(gen_dir) / FAILED_FILENAME, payload)
    except Exception:
        pass


def _generation_invalidation_marker(gen_dir: Path, reason: str, previous_generation: str | None = None) -> bool:
    try:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "kind": "pnw_cache_generation_invalidated",
            "generation": Path(gen_dir).name,
            "invalidated_utc": _utc_now(),
            "reason": reason,
            "previous_active_generation": previous_generation,
        }
        _atomic_write_json(Path(gen_dir) / INVALIDATED_FILENAME, payload)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Modifier bake helpers (thin seams for tests and failure handling)


def _run_bake_operator(obj: bpy.types.Object, mod: bpy.types.Modifier, bake_id: int, scene: bpy.types.Scene) -> set[str]:
    with bpy.context.temp_override(object=obj, scene=scene):
        return set(bpy.ops.object.geometry_node_bake_single(
            session_uid=obj.session_uid,
            modifier_name=mod.name,
            bake_id=int(bake_id),
        ))


def _delete_bake_operator(obj: bpy.types.Object, mod: bpy.types.Modifier, bake_id: int) -> set[str]:
    with bpy.context.temp_override(object=obj):
        return set(bpy.ops.object.geometry_node_bake_delete_single(
            session_uid=obj.session_uid,
            modifier_name=mod.name,
            bake_id=int(bake_id),
        ))


def _detach_bakes(obj: bpy.types.Object, mod: bpy.types.Modifier) -> tuple[int, list[str], list[str]]:
    """Clear in-memory bake data without deleting the old cache directory.

    Blender's delete operator uses the modifier's *current* bake_directory.  A
    disposable scratch directory is installed first, so even if the operator
    removes disk data it cannot touch the retained generation.
    """
    deleted = 0
    errors: list[str] = []
    removed_paths: list[str] = []
    scratch = Path(tempfile.mkdtemp(prefix="pnw-detach-"))
    try:
        mod.bake_directory = str(scratch)
        try:
            bakes = list(mod.bakes)
        except Exception:
            bakes = []
        for bake in bakes:
            try:
                if bool(getattr(bake, "use_custom_path", False)):
                    bake.use_custom_path = False
                    bake.directory = ""
            except Exception:
                pass
            try:
                result = _delete_bake_operator(obj, mod, int(bake.bake_id))
                if "FINISHED" in result:
                    deleted += 1
                else:
                    errors.append(f"bake {bake.bake_id}: {sorted(result)}")
            except Exception as exc:
                errors.append(f"bake {bake.bake_id}: {type(exc).__name__}: {exc}")
    finally:
        mod.bake_directory = ""
    try:
        if _safe_exists(scratch):
            # Non-recursive: only an empty scratch directory is removed.
            os.rmdir(scratch)
            removed_paths.append(str(scratch))
    except OSError:
        pass
    return deleted, errors, removed_paths


# ---------------------------------------------------------------------------
# Public lifecycle functions


def _validate_generation_manifest_identity(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    gen_dir: Path,
    root: Path | None,
    manifest: dict[str, Any],
    params_hash: str,
    graph_fingerprint: str,
) -> tuple[bool, str, Path]:
    """Validate one concrete generation's identity/files, independent of root CURRENT."""
    if root is not None:
        marker = _read_owner_marker(root)
        if marker is not None and marker.get("owner_token") != manifest.get("owner_token"):
            return False, "cache_owner_token_mismatch", gen_dir

    current_identity = pnw_identity.build_cache_identity(
        obj,
        mod,
        params_hash=params_hash,
        graph_fingerprint=graph_fingerprint,
        frame_start=int(manifest.get("frame_start", getattr(bpy.context.scene, "frame_start", 1))),
        frame_end=int(manifest.get("frame_end", getattr(bpy.context.scene, "frame_end", 1))),
    )
    reliable = bool((current_identity.get("reliability") or {}).get("reliable"))
    if not reliable:
        reasons = ",".join((current_identity.get("reliability") or {}).get("reasons") or [])
        return False, f"unreliable_dependency:{reasons}", gen_dir
    old_identity = manifest.get("identity")
    if not isinstance(old_identity, dict):
        return False, "cache_manifest_corrupt", gen_dir
    if not bool((old_identity.get("reliability") or {}).get("reliable")):
        return False, "cache_baked_with_unreliable_identity", gen_dir
    if manifest.get("identity_hash") != current_identity.get("identity_hash"):
        return False, pnw_identity.identity_reason(old_identity, current_identity), gen_dir
    if params_hash and manifest.get("parameters_hash") != str(params_hash):
        return False, "parameters_changed", gen_dir
    if graph_fingerprint:
        accepted_graphs = {
            str(manifest.get("caller_graph_fingerprint") or ""),
            str(manifest.get("semantic_graph_fingerprint") or ""),
        }
        if str(graph_fingerprint) not in accepted_graphs:
            return False, "graph_fingerprint_changed", gen_dir

    valid, file_reason, _details = _verify_generation_files(gen_dir, manifest)
    if not valid:
        return False, file_reason or "cache_integrity_failed", gen_dir
    return True, "valid", gen_dir


def is_cache_valid(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    params_hash: str,
    graph_fingerprint: str,
) -> tuple[bool, str, Path]:
    """Validate the active generation without creating or deleting anything."""
    candidate = resolve_bake_directory(obj, mod, getattr(mod, "bake_directory", None) or None)
    try:
        if mod is None or getattr(mod, "type", None) != "NODES":
            return False, "modifier_missing", candidate
        kind, root, reason = _classify_candidate(candidate, obj, mod, readonly=True)
        if kind == "generation":
            if root is None or reason:
                return False, reason or "cache_owner_unverified", candidate
            if (candidate / INVALIDATED_FILENAME).is_file():
                return False, "cache_generation_invalidated", candidate
            manifest, manifest_reason = _read_generation_manifest(candidate)
            if manifest is None:
                return False, manifest_reason or "cache_manifest_missing", candidate
            # Explicit generation path: Blender consumes this generation
            # directly.  Do not let a newer root CURRENT invalidate it.
            return _validate_generation_manifest_identity(
                obj,
                mod,
                candidate,
                root,
                manifest,
                params_hash,
                graph_fingerprint,
            )

        if kind == "partial_generation":
            return False, reason or "cache_manifest_missing", candidate

        if kind == "root" and root is not None:
            if reason:
                return False, reason, candidate
            current = _read_current(root)
            if current is None:
                return False, "cache_pointer_missing", candidate
            status = str(current.get("status"))
            if status == "invalidated":
                return False, "cache_invalidated", candidate
            if status != "committed":
                return False, "cache_pointer_corrupt", candidate
            generation = current.get("generation")
            if not isinstance(generation, str) or _safe_rel_path(generation) is None or Path(generation).name != generation:
                return False, "cache_pointer_corrupt", candidate
            gen_dir = root / GENERATIONS_DIR_NAME / generation
            if not gen_dir.is_dir():
                return False, "cache_generation_missing", candidate
            if (gen_dir / INVALIDATED_FILENAME).is_file():
                return False, "cache_generation_invalidated", gen_dir

            manifest, manifest_reason = _read_generation_manifest(gen_dir)
            if manifest is None:
                return False, manifest_reason or "cache_manifest_missing", gen_dir
            if current.get("manifest_sha256") != manifest.get("manifest_sha256"):
                return False, "cache_pointer_manifest_mismatch", gen_dir
            if current.get("identity_hash") != manifest.get("identity_hash"):
                return False, "cache_pointer_identity_mismatch", gen_dir
            return _validate_generation_manifest_identity(
                obj,
                mod,
                gen_dir,
                root,
                manifest,
                params_hash,
                graph_fingerprint,
            )

        if reason in {"legacy_cache_manifest", "legacy_cache_layout"}:
            return False, "legacy_cache_manifest_without_identity", candidate
        if reason:
            return False, reason, candidate
        if kind == "uncommitted_root":
            return False, "cache_pointer_missing", candidate
        if (candidate / MANIFEST_FILENAME).is_file():
            return False, "cache_pointer_missing", candidate
        if _safe_exists(candidate):
            return False, "manifest_missing", candidate
        return False, "cache_not_found", candidate
    except CacheError as exc:
        return False, exc.code, candidate
    except pnw_identity.CacheIdentityError as exc:
        return False, exc.code, candidate
    except Exception as exc:
        return False, f"cache_validation_error:{type(exc).__name__}", candidate


def invalidate_cache(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    requested_dir: str | None = None,
) -> dict[str, Any]:
    """Detach the modifier's cache reference without deleting user data.

    Only the explicitly requested path is examined.  Unowned, legacy or foreign
    directories are refused; a sentinel file in such a directory is never
    touched.  For an owned cache the current pointer is atomically marked
    invalidated and the generation directory is retained on disk.
    """
    explicit = _path(requested_dir)
    candidate = resolve_bake_directory(obj, mod, requested_dir)
    result: dict[str, Any] = {
        "invalidated": False,
        "bake_directory": str(candidate),
        "deleted_bakes": 0,
        "deleted_paths": [],
        "retained_directory": _safe_exists(candidate),
        "owner_verified": False,
    }
    try:
        kind, root, reason = _classify_candidate(candidate, obj, mod)
    except Exception as exc:
        result.update({"reason": f"classification_error:{type(exc).__name__}", "message": str(exc)})
        return result

    current_dir = _path(getattr(mod, "bake_directory", None))
    is_current = current_dir is not None and _path_key(current_dir) == _path_key(candidate)

    if kind in {"root", "generation", "partial_generation"} and root is not None:
        if reason:
            result.update({"reason": reason, "owner_verified": False})
            return result
        result["owner_verified"] = True
        result["owner_root"] = str(root)
        current = _read_current(root)
        active_generation = current.get("generation") if current is not None else None
        if kind in {"generation", "partial_generation"}:
            target_generation = candidate.name if _safe_rel_path(candidate.name) is not None else None
        else:
            target_generation = active_generation
        affects_active_root = kind == "root" or (
            target_generation is not None and target_generation == active_generation
        )
        pointer_updated = False
        generation_invalidated = False
        if affects_active_root:
            try:
                _write_current(
                    root,
                    {
                        "status": "invalidated",
                        "owner_token": (_read_owner_marker(root) or {}).get("owner_token"),
                        "generation": active_generation,
                        "manifest_sha256": (current or {}).get("manifest_sha256"),
                        "identity_hash": (current or {}).get("identity_hash"),
                        "invalidated_utc": _utc_now(),
                        "reason": "invalidate_cache",
                    },
                )
                pointer_updated = True
            except OSError as exc:
                result.update({"reason": "cache_pointer_write_failed", "message": str(exc)})
                return result
        if target_generation:
            target_dir = root / GENERATIONS_DIR_NAME / target_generation
            if _safe_is_dir(target_dir):
                generation_invalidated = _generation_invalidation_marker(
                    target_dir,
                    "invalidate_cache",
                    active_generation,
                )
        if not affects_active_root and not generation_invalidated:
            result.update(
                {
                    "reason": "generation_invalidation_write_failed",
                    "retained_generation": target_generation,
                    "active_generation": active_generation,
                }
            )
            return result
        try:
            deleted, errors, removed_paths = _detach_bakes(obj, mod)
        except Exception as exc:
            deleted, errors, removed_paths = 0, [f"detach_error:{type(exc).__name__}:{exc}"], []
        _store_last_root(obj, mod, root)
        result.update(
            {
                "invalidated": True,
                "bake_directory": str(root / GENERATIONS_DIR_NAME / str(target_generation)) if target_generation else str(root),
                "deleted_bakes": deleted,
                "deleted_paths": removed_paths,
                "cache_files_retained": True,
                "retained_generation": target_generation,
                "active_generation": active_generation,
                "generation_invalidated": generation_invalidated,
                "pointer_updated": pointer_updated,
                "reason": "invalidated_owned_cache" if pointer_updated else "invalidated_generation_only",
            }
        )
        if errors:
            result["detach_errors"] = errors
        return result

    if kind in {"legacy", "foreign", "missing", "file", "managed_root", "uncommitted_root", "generation", "partial_generation"}:
        if explicit is not None and not is_current:
            result.update(
                {
                    "reason": "unowned_directory_refused",
                    "owner_verified": False,
                    "retained_directory": _safe_exists(candidate),
                }
            )
            return result
        # The modifier currently references a legacy/unowned path.  Detach the
        # in-memory bake data, keep every file, and only clear the path.
        if kind == "file":
            result.update({"reason": "cache_path_is_file"})
            return result
        try:
            deleted, errors, removed_paths = _detach_bakes(obj, mod)
        except Exception as exc:
            deleted, errors, removed_paths = 0, [f"detach_error:{type(exc).__name__}:{exc}"], []
        _store_last_root(obj, mod, None)
        result.update(
            {
                "invalidated": True,
                "bake_directory": str(candidate),
                "deleted_bakes": deleted,
                "deleted_paths": removed_paths,
                "cache_files_retained": True,
                "pointer_updated": False,
                "reason": "detached_unowned_or_legacy_cache_without_deleting",
            }
        )
        if errors:
            result["detach_errors"] = errors
        return result

    result["reason"] = reason or "unowned_directory_refused"
    return result


def execute_disk_bake(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    frame_start: int,
    frame_end: int,
    params_hash: str,
    graph_fingerprint: str,
    requested_dir: str | None = None,
) -> dict[str, Any]:
    """Bake into a fresh, verified generation and commit it atomically.

    The old active generation is never deleted.  On failure the modifier's
    previous bake directory is restored and the previous current pointer is
    left (or restored) untouched.
    """
    if mod is None:
        raise CacheError("modifier_missing", "Cannot bake without a modifier")
    if int(frame_end) < int(frame_start):
        raise CacheError("invalid_frame_range", f"Invalid frame range: {frame_start}..{frame_end}")

    scene = bpy.context.scene
    if scene is None:
        raise CacheError("scene_missing", "Cannot bake without an active scene")
    scene.frame_start = int(frame_start)
    scene.frame_end = int(frame_end)
    try:
        scene.frame_step = 1
    except Exception:
        pass
    bpy.context.view_layer.update()

    # Reject unreliable inputs before creating any directory.
    preliminary_identity = pnw_identity.build_cache_identity(
        obj,
        mod,
        params_hash=params_hash,
        graph_fingerprint=graph_fingerprint,
        frame_start=frame_start,
        frame_end=frame_end,
    )
    if not bool((preliminary_identity.get("reliability") or {}).get("reliable")):
        reasons = (preliminary_identity.get("reliability") or {}).get("reasons") or []
        raise pnw_identity.CacheIdentityError(
            "unreliable_cache_identity",
            "Refusing to bake: cache identity cannot be guaranteed (" + ", ".join(reasons) + ")",
            details={"reasons": reasons},
        )

    candidate = resolve_bake_directory(obj, mod, requested_dir)
    workflow_binding = _ensure_workflow_binding(obj, mod)
    root = _ensure_owner_root(candidate, obj, mod, workflow_binding)
    owner_marker = _read_owner_marker(root)
    owner_token = (owner_marker or {}).get("owner_token")
    if not isinstance(owner_token, str):
        raise CacheError("cache_owner_unverified", f"Owner token missing for {root}")
    owner_ok, owner_reason, _owner_payload = _verify_owner_marker(
        root,
        obj,
        mod,
        expected_binding=workflow_binding,
    )
    if not owner_ok:
        raise CacheError(
            "cache_owner_unverified",
            f"Owner marker does not match the current workflow: {owner_reason}",
            details={"path": str(root)},
        )

    previous_bake_directory = getattr(mod, "bake_directory", "")
    previous_bake_target = getattr(mod, "bake_target", None)
    previous_current = _snapshot_current_bytes(root)

    generation = _create_generation_dir(root)
    committed_pointer: bytes | None = None
    pointer_committed = False
    identity_hash = preliminary_identity.get("identity_hash")
    try:
        mod.bake_target = "DISK"
        mod.bake_directory = str(generation)
        bpy.context.view_layer.update()

        # Rebuild after changing storage settings; identity excludes the path and
        # target, but this also detects a changing modifier between the two reads.
        pre_bake_identity = pnw_identity.build_cache_identity(
            obj,
            mod,
            params_hash=params_hash,
            graph_fingerprint=graph_fingerprint,
            frame_start=frame_start,
            frame_end=frame_end,
        )
        if not bool((pre_bake_identity.get("reliability") or {}).get("reliable")):
            reasons = (pre_bake_identity.get("reliability") or {}).get("reasons") or []
            raise pnw_identity.CacheIdentityError(
                "unreliable_cache_identity",
                "Refusing to bake: cache identity changed before baking (" + ", ".join(reasons) + ")",
                details={"reasons": reasons},
            )
        if pre_bake_identity.get("identity_hash") != preliminary_identity.get("identity_hash"):
            raise CacheError("cache_identity_changed_before_bake", "Cache identity changed while preparing the bake")
        identity_hash = pre_bake_identity.get("identity_hash")

        bakes = list(mod.bakes)
        if not bakes:
            raise CacheError("no_bake_zones", f"Modifier '{mod.name}' exposes zero simulation/bake zones")

        delete_errors: list[str] = []
        for bake in list(bakes):
            try:
                if bool(getattr(bake, "use_custom_path", False)):
                    bake.use_custom_path = False
                    bake.directory = ""
            except Exception:
                pass
            try:
                result = _delete_bake_operator(obj, mod, int(bake.bake_id))
                if "FINISHED" not in result:
                    delete_errors.append(f"bake {bake.bake_id}: {sorted(result)}")
            except Exception as exc:
                delete_errors.append(f"bake {bake.bake_id}: {type(exc).__name__}: {exc}")
        if delete_errors:
            raise CacheError("bake_reset_failed", "Could not clear previous in-memory bake data", details={"errors": delete_errors})

        bakes = list(mod.bakes)
        if not bakes:
            raise CacheError("no_bake_zones", f"Modifier '{mod.name}' exposes zero simulation/bake zones after reset")

        baked_ids: list[int] = []
        for bake in bakes:
            bake.use_custom_simulation_frame_range = True
            bake.frame_start = int(frame_start)
            bake.frame_end = int(frame_end)
            result = _run_bake_operator(obj, mod, int(bake.bake_id), scene)
            if "FINISHED" not in result:
                raise CacheError("bake_operator_failed", f"Bake failed for bake_id={bake.bake_id}: {sorted(result)}")
            baked_ids.append(int(bake.bake_id))

        scan = _scan_generation(generation, baked_ids, frame_start, frame_end)

        post_bake_identity = pnw_identity.build_cache_identity(
            obj,
            mod,
            params_hash=params_hash,
            graph_fingerprint=graph_fingerprint,
            frame_start=frame_start,
            frame_end=frame_end,
        )
        if not bool((post_bake_identity.get("reliability") or {}).get("reliable")):
            reasons = (post_bake_identity.get("reliability") or {}).get("reasons") or []
            raise pnw_identity.CacheIdentityError(
                "unreliable_cache_identity",
                "Cache identity became unreliable during bake (" + ", ".join(reasons) + ")",
                details={"reasons": reasons},
            )
        if post_bake_identity.get("identity_hash") != pre_bake_identity.get("identity_hash"):
            raise CacheError("cache_identity_changed_during_bake", "Cache identity changed during bake")
        identity_hash = post_bake_identity.get("identity_hash")

        manifest = _manifest_payload(
            generation,
            owner_token,
            post_bake_identity,
            params_hash,
            graph_fingerprint,
            scan,
            frame_start,
            frame_end,
        )
        _atomic_write_json(generation / MANIFEST_FILENAME, manifest)

        written_manifest, manifest_reason = _read_generation_manifest(generation)
        if written_manifest is None:
            raise CacheError(manifest_reason or "cache_manifest_write_failed", "Committed manifest cannot be read back")
        valid, file_reason, _details = _verify_generation_files(generation, written_manifest)
        if not valid:
            raise CacheError(file_reason or "cache_integrity_failed", "Baked files failed integrity verification")

        current_payload = _write_current(
            root,
            {
                "status": "committed",
                "owner_token": owner_token,
                "generation": generation.name,
                "manifest_sha256": written_manifest["manifest_sha256"],
                "identity_hash": written_manifest["identity_hash"],
                "committed_utc": _utc_now(),
            },
        )
        committed_pointer = (root / CURRENT_FILENAME).read_bytes()
        pointer_committed = True
        _store_last_root(obj, mod, root)

        return {
            "baked": True,
            "bake_directory": str(generation),
            "baked_ids": baked_ids,
            "manifest": written_manifest,
            "generation": generation.name,
            "owner_root": str(root),
            "identity_hash": written_manifest["identity_hash"],
            "manifest_sha256": written_manifest["manifest_sha256"],
            "files_verified": len(written_manifest["files"]),
            "previous_bake_directory": str(previous_bake_directory) if previous_bake_directory else None,
            "current": current_payload,
        }
    except BaseException as exc:
        _failure_marker(generation, exc, identity_hash if isinstance(identity_hash, str) else None)
        try:
            mod.bake_directory = previous_bake_directory
            if previous_bake_target is not None:
                mod.bake_target = previous_bake_target
            bpy.context.view_layer.update()
        except Exception:
            pass
        if pointer_committed:
            try:
                current_bytes = (root / CURRENT_FILENAME).read_bytes()
            except OSError:
                current_bytes = None
            if committed_pointer is None or current_bytes == committed_pointer:
                _restore_current_bytes(root, previous_current)
        raise
