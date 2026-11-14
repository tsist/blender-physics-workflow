"""Portable, read-only delivery verification shared by Blender and the CLI."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

SCHEMA = "pnw.export.delivery/1"
FORMATS = {"STATIC_MESH", "ALEMBIC", "OPENVDB"}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_record(path, role=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"EXPORT_DELIVERY_INVALID: missing or symbolic file: {path}")
    record = {"path": str(path.resolve()), "sha256": sha256_file(path), "size": path.stat().st_size}
    if role:
        record["role"] = role
    return record


def manifest_path(primary):
    return Path(str(primary) + ".pnw-export.json")


def ensure_new_manifest(primary):
    path = manifest_path(primary)
    if path.exists() or path.is_symlink():
        raise ValueError(f"Export manifest already exists; choose a new path: {path}")


def write_manifest(primary, export_format, files, preservation):
    path = manifest_path(primary)
    payload = {"schema": SCHEMA, "export_format": export_format,
               "primary": file_record(primary), "files": files, "preservation": preservation}
    # Exclusive creation protects previous evidence, including a concurrent writer.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    ref = {"path": str(path), "sha256": sha256_file(path), "schema": SCHEMA}
    verify_manifest(ref, primary, export_format)
    return ref


def _check_record(record):
    if not isinstance(record, dict):
        raise ValueError("EXPORT_DELIVERY_INVALID: invalid file record")
    value, digest, size = (record.get(k) for k in ("path", "sha256", "size"))
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(size) is not int or size < 0):
        raise ValueError("EXPORT_DELIVERY_INVALID: invalid file path/hash/size")
    actual = file_record(value)
    if actual["sha256"] != digest or actual["size"] != size:
        raise ValueError(f"EXPORT_DELIVERY_INVALID: changed file: {value}")
    return Path(actual["path"])


def verify_manifest(ref, primary, export_format):
    """Verify references only. Never copy, delete or rewrite a referenced file."""
    if not isinstance(ref, dict) or ref.get("schema") != SCHEMA:
        raise ValueError("EXPORT_DELIVERY_INVALID: missing/current-schema manifest required")
    value, digest = ref.get("path"), ref.get("sha256")
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("EXPORT_DELIVERY_INVALID: invalid manifest reference")
    path = Path(value)
    expected = Path(primary).resolve()
    if path.resolve() != manifest_path(expected) or path.is_symlink() or not path.is_file():
        raise ValueError("EXPORT_DELIVERY_INVALID: missing or mismatched sidecar path")
    if path.stat().st_size > 4 * 1024 * 1024 or sha256_file(path) != digest:
        raise ValueError("EXPORT_DELIVERY_INVALID: manifest hash/size mismatch")
    def reject_constant(value):
        raise ValueError(f"EXPORT_DELIVERY_INVALID: non-finite JSON constant {value}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("EXPORT_DELIVERY_INVALID: malformed or excessively nested JSON") from exc
    if (not isinstance(payload, dict) or payload.get("schema") != SCHEMA
            or export_format not in FORMATS or payload.get("export_format") != export_format
            or not isinstance(payload.get("preservation"), dict)
            or not isinstance(payload.get("files"), list)):
        raise ValueError("EXPORT_DELIVERY_INVALID: invalid manifest content")
    if _check_record(payload.get("primary")) != expected:
        raise ValueError("EXPORT_DELIVERY_INVALID: primary path mismatch")
    seen = {expected, path.resolve()}
    for record in payload["files"]:
        actual = _check_record(record)
        if actual in seen or not isinstance(record.get("role"), str) or not record["role"]:
            raise ValueError("EXPORT_DELIVERY_INVALID: duplicate/self-reference or missing dependency role")
        seen.add(actual)
    return payload


def verify_export_result(info, export_format, primary):
    if export_format in (None, "NONE"):
        return None
    if not isinstance(info, dict) or info.get("exported") is not True or info.get("export_format") != export_format:
        raise ValueError("EXPORT_DELIVERY_INVALID: missing or mismatched export result")
    if (not primary and export_format == "STATIC_MESH"
            and not info.get("saved_path") and not info.get("export_path") and not info.get("delivery_manifest")):
        return None  # An explicitly in-scene static object has no disk delivery.
    reported = info.get("saved_path") or info.get("export_path")
    if not primary or not isinstance(reported, str) or Path(reported).resolve() != Path(primary).resolve():
        raise ValueError("EXPORT_DELIVERY_INVALID: exported path differs from request")
    return verify_manifest(info.get("delivery_manifest"), primary, export_format)
