# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic source, extension, and skill archives from PUBLICATION.json."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
import zipfile

sys.dont_write_bytecode = True
from verify_release import ADDON_DIR, ROOT, load_publication, release_files, safe_file, sha, verify


def archive(entries: list[tuple[str, bytes]]) -> bytes:
    if len(entries) != len({name for name, _ in entries}):
        raise ValueError("Duplicate archive member")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as output:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            output.writestr(info, data)
    return buffer.getvalue()


def build(out: Path) -> dict:
    checked = verify()
    pub = load_publication()
    source = [(name, safe_file(ROOT, name).read_bytes()) for name in release_files(pub)]
    license_data = safe_file(ROOT, "LICENSE").read_bytes()

    addon_entries = [(n[len(ADDON_DIR):], b) for n, b in source if n.startswith(ADDON_DIR)]
    skill_entries = [(n[len("skills/"):], b) for n, b in source if n.startswith("skills/blender-physics-workflow/")]
    skill_entries.append(("LICENSE", license_data))

    bundles = {
        f"{pub['repository']}-{pub['release']}-source.zip": source,
        f"physics_nodes_workflow_addon-{pub['addon']}.zip": addon_entries,
        f"blender-physics-workflow-skill-{pub['skill']}.zip": skill_entries,
    }

    if out.is_symlink():
        raise ValueError("Refusing symlink output")
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, entries in bundles.items():
        data = archive(entries)
        if data != archive(entries):
            raise ValueError("Reproducibility failure: " + name)
        path = out / name
        if path.exists() and path.read_bytes() != data:
            raise ValueError("Refusing to overwrite differing artifact: " + name)
        if not path.exists():
            path.write_bytes(data)
        with zipfile.ZipFile(path) as zf:
            if zf.namelist() != sorted(n for n, _ in entries) or any(zf.read(n) != b for n, b in entries):
                raise ValueError("Archive member mismatch: " + name)
        rows.append({"file": name, "sha256": sha(data), "bytes": len(data), "entries": len(entries)})

    sums = "".join(f"{row['sha256']}  {row['file']}\n" for row in sorted(rows, key=lambda r: r["file"])).encode("utf-8")
    checksum = out / "SHA256SUMS"
    if checksum.exists() and checksum.read_bytes() != sums:
        raise ValueError("Refusing to overwrite differing SHA256SUMS")
    if not checksum.exists():
        checksum.write_bytes(sums)

    return {"status": "passed", "validation": checked, "artifacts": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--verify", action="store_true", help="Accepted for CI parity; every build verifies bytes")
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), indent=2))
