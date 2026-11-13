# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify public inventory, runtime identity, skill links, and privacy exclusions."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = "tools/physics_nodes_workflow_addon/"
CLI_DIR = "tools/physics_workflow_cli_v1/"
BLOCKED = {".git", "__pycache__", ".venv", "audit", "research", "test-output", "dist", "runtime"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_file(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if not name or "\\" in name or ":" in name or name.startswith("/") or (set(parts) & (BLOCKED | {".."})):
        raise ValueError("Unsafe path: " + name)
    path = root.joinpath(*parts)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Not an internal regular file: " + name)
    return path


def load_publication(root: Path = ROOT) -> dict:
    return json.loads((root / "PUBLICATION.json").read_text(encoding="utf-8"))


def release_files(publication: dict) -> list[str]:
    return sorted(["PUBLICATION.json"] + [row["file"] for row in publication["files"]])


def runtime_entries(root: Path = ROOT) -> list[dict[str, str]]:
    paths = set((root / CLI_DIR).glob("*.py"))
    paths.update((root / ADDON_DIR).glob("*.py"))
    paths.add(root / ADDON_DIR / "blender_manifest.toml")
    return [
        {"file": p.relative_to(root).as_posix(), "expected_sha256": sha(p.read_bytes())}
        for p in sorted(paths)
    ]


def private_findings(text: str) -> bool:
    normalized = text.replace("\\", "/").lower()
    patterns = [
        r"gh[pousr]_[A-Za-z0-9]{30,}",
        r"github_pat_[A-Za-z0-9_]{40,}",
        r"sk-[A-Za-z0-9_-]{32,}",
        r"AKIA[A-Z0-9]{16}",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    ]
    return bool(
        (("173" + "99") in normalized)
        or re.search(r"f:/+blender ai|wm-20\d\d-\d\d-\d\d-|runtime/+audit/", normalized)
        or any(re.search(p, text) for p in patterns)
    )


def verify(root: Path = ROOT) -> dict:
    publication = load_publication(root)
    names = release_files(publication)
    if len(names) != len(set(names)):
        raise ValueError("Duplicate inventory path")

    for row in publication["files"]:
        if sha(safe_file(root, row["file"]).read_bytes()) != row["sha256"]:
            raise ValueError("Inventory hash mismatch: " + row["file"])

    runtime = runtime_entries(root)
    identity = sha(json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if len(runtime) != 18 or identity != publication["runtime_identity"]:
        raise ValueError(f"18-file physics runtime identity mismatch (got {len(runtime)} files, {identity})")
    if not {row["file"] for row in runtime} <= set(names):
        raise ValueError("Runtime files missing from release inventory")

    for name in names:
        path = safe_file(root, name)
        if path.suffix.lower() in {".pyc", ".blend", ".blend1", ".exe", ".dll"} or path.name in {"AGENTS.md", ".env", "secrets.json"}:
            raise ValueError("Forbidden release file: " + name)
        text = path.read_text(encoding="utf-8-sig")
        if private_findings(text):
            raise ValueError("Private content pattern in: " + name)
        if path.suffix == ".py":
            ast.parse(text, filename=name)
        if path.suffix == ".json":
            json.loads(text)

    for skill_md in (root / "skills").glob("*/SKILL.md"):
        body = skill_md.read_text(encoding="utf-8-sig")
        if not body.startswith("---\n") or not re.search(r"^name: " + re.escape(skill_md.parent.name) + r"\s*$", body, re.M):
            raise ValueError("Invalid skill frontmatter: " + str(skill_md))

    if (root / ".git").exists():
        result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
        tracked = {p.decode("utf-8") for p in result.stdout.split(b"\0") if p}
        if tracked and tracked != set(names):
            raise ValueError("Git tracked files differ from release inventory: " + repr(sorted(tracked ^ set(names))))

    return {
        "status": "passed",
        "runtime_files": len(runtime),
        "runtime_identity": identity,
        "release_files": len(names),
        "scope": "Source hashes, syntax, explicit inventory, skill frontmatter and finite privacy checks",
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify(), indent=2))
    except (ValueError, OSError, KeyError, SyntaxError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        sys.exit(1)
