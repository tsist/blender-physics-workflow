"""Filesystem identity and overwrite guards shared by the CLI runner and worker.

The guards deliberately operate on canonical paths and, when both sides exist,
on real file identity (``samefile`` / ``st_dev`` + ``st_ino``).  This catches
case-only aliases, ``..`` aliases, symlinks and hardlinks before Blender is
allowed to open an input file or create an output file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable


class PathGuardError(ValueError):
    """A structured path-safety violation."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_error(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return error


def normalize_path(value: str | os.PathLike[str]) -> Path:
    """Return an absolute, symlink-resolved path without requiring it to exist."""

    if isinstance(value, bool) or value is None:
        raise PathGuardError("INVALID_PATH", "Path must be a non-empty string")
    try:
        text = os.fspath(value)
    except TypeError as exc:
        raise PathGuardError("INVALID_PATH", f"Path must be a string or path-like object: {value!r}") from exc
    if not isinstance(text, str) or not text.strip():
        raise PathGuardError("INVALID_PATH", "Path must be a non-empty string")
    if "\x00" in text:
        raise PathGuardError("INVALID_PATH", "Path contains a NUL byte")
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathGuardError("INVALID_PATH", f"Path cannot be resolved: {text}: {exc}") from exc


def path_key(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(str(normalize_path(value))))


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)))


def same_file(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    """True when two paths name the same file, including hardlinks/symlinks."""

    left_path = normalize_path(left)
    right_path = normalize_path(right)
    if path_key(left_path) == path_key(right_path):
        return True
    if not left_path.exists() or not right_path.exists():
        return False
    try:
        if os.path.samefile(left_path, right_path):
            return True
    except OSError:
        pass
    left_identity = _file_identity(left_path)
    right_identity = _file_identity(right_path)
    return left_identity is not None and left_identity == right_identity


def overwrite_authorized(raw: Any) -> bool:
    """Only an explicit JSON boolean ``true`` authorizes replacement.

    ``allow_overwrite`` is the canonical field.  ``overwrite`` is accepted as a
    compatibility alias, but truthy strings/numbers are never accepted.
    """

    if not isinstance(raw, dict):
        return False
    return raw.get("allow_overwrite") is True or raw.get("overwrite") is True


def guard_output_path(
    input_blend: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    allow_overwrite: bool,
    reserved_paths: Iterable[str | os.PathLike[str]] = (),
    role: str = "output_blend",
) -> Path:
    """Reject output aliases, protected paths and unauthorized replacements.

    The input file is never allowed to be replaced, even when
    ``allow_overwrite`` is true.  A pre-existing output is rejected unless the
    caller supplied explicit authorization.
    """

    input_path = normalize_path(input_blend)
    output_path = normalize_path(output_path)

    if same_file(input_path, output_path):
        raise PathGuardError(
            "PATH_CONFLICT",
            f"{role} resolves to input_blend (same path, symlink or hardlink)",
            input_blend=str(input_path),
            requested_output=str(output_path),
            role=role,
        )

    for reserved in reserved_paths:
        if same_file(output_path, reserved):
            raise PathGuardError(
                "PATH_CONFLICT",
                f"{role} resolves to a reserved job file",
                requested_output=str(output_path),
                reserved_path=str(normalize_path(reserved)),
                role=role,
            )

    if output_path.exists():
        if output_path.is_dir():
            raise PathGuardError(
                "INVALID_OUTPUT",
                f"{role} is an existing directory: {output_path}",
                requested_output=str(output_path),
                role=role,
            )
        if not allow_overwrite:
            raise PathGuardError(
                "OUTPUT_EXISTS",
                f"{role} already exists; pass allow_overwrite=true to replace it",
                requested_output=str(output_path),
                role=role,
            )

    return output_path
