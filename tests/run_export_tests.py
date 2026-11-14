"""Run real Blender export tests in isolated fresh processes.

Usage: python tests/run_export_tests.py (set BLENDER_PATH as needed).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
BLENDER = Path(os.environ.get("BLENDER_PATH", ROOT / "runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe"))
TESTS = ROOT / "tests"
EVIDENCE = Path(os.environ.get("PNW_TEST_OUTPUT", str(ROOT / "test-output/physics-exports")))


class Failure(RuntimeError):
    pass


def run_blender(script, args, workdir, name, env):
    log = workdir / f"{name}.log"
    cmd = [
        str(BLENDER),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python-exit-code",
        "1",
        "--python",
        str(TESTS / script),
        "--",
        *[str(arg) for arg in args],
    ]
    completed = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    log.write_text(
        f"COMMAND: {cmd}\n\nSTDOUT:\n{completed.stdout}\n\nSTDERR:\n{completed.stderr}\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        tail = "\n".join((completed.stdout + "\n" + completed.stderr).splitlines()[-40:])
        raise Failure(f"{name} exited {completed.returncode}\n{tail}")
    return completed


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def require(condition, message, checks):
    checks.append({"ok": bool(condition), "message": message})
    if not condition:
        raise Failure(message)


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = EVIDENCE / f"export-run-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=False)
    config = run_dir / "blender-config"
    extensions = run_dir / "blender-extensions"
    config.mkdir()
    extensions.mkdir()
    env = dict(os.environ)
    env["BLENDER_USER_CONFIG"] = str(config)
    env["BLENDER_USER_EXTENSIONS"] = str(extensions)

    checks = []
    summary = {"run_dir": str(run_dir), "checks": checks, "artifacts": {}}

    vdb_dir = run_dir / "openvdb"
    vdb_dir.mkdir()
    vdb_json = vdb_dir / "produce.json"
    run_blender(
        "openvdb_case_produce.py",
        ["--workdir", vdb_dir, "--out", vdb_json],
        run_dir,
        "openvdb_produce",
        env,
    )
    produce = load(vdb_json)
    success = produce["success"]
    require(success["ok"], "VDB success case completed", checks)
    require(success["result"]["exported"] is True, "VDB result exported", checks)
    require(
        success["result"]["semantics"] == "surface_level_set_of_realized_world_mesh",
        "VDB semantics are a realized surface level set",
        checks,
    )
    require(
        success["result"]["native_volume_density_exported"] is False,
        "VDB does not claim native volume density",
        checks,
    )
    require(
        success["readback"]["count"] == 1
        and success["readback"]["type"] == "FloatGrid"
        and success["readback"]["name"] == "surface"
        and success["readback"]["grid_class"] == "level set",
        "independent openvdb.readAll finds one FLOAT LEVEL_SET grid",
        checks,
    )
    require(success["readback"]["active_voxels"] > 0, "VDB has active voxels", checks)
    require(success["readback"]["finite"], "VDB voxels are finite", checks)
    require(
        success["readback"]["negative_voxels"] > 0 and success["readback"]["positive_voxels"] > 0,
        "VDB has both interior and exterior level-set values",
        checks,
    )
    low = success["readback"]["world_bbox"][0]
    high = success["readback"]["world_bbox"][1]
    expected_min = success["expected_world_min"]
    expected_max = success["expected_world_max"]
    require(
        all(low[i] <= expected_min[i] + 0.2 and high[i] >= expected_max[i] - 0.2 for i in range(3)),
        "VDB world bounds contain the rotated/non-uniformly scaled world mesh",
        checks,
    )
    surface_probes = success["world_probes"]["surface"]
    require(
        all(
            probe["value"] is not None and abs(probe["value"]) <= 0.16
            for probe in surface_probes
        ),
        "VDB zero level set lies on the rotated/scaled world surface samples",
        checks,
    )
    require(
        success["world_probes"]["center"]["value"] is not None
        and success["world_probes"]["center"]["value"] < 0.0,
        "VDB center sample is negative (inside the world-space level set)",
        checks,
    )
    require(
        success["blender_volume"]["grids"] == [["surface", "FLOAT"]]
        and not success["blender_volume"]["grids_error"],
        "Blender Volume loads the VDB grid",
        checks,
    )
    require(not success["temp_files"], "no temporary VDB files remain", checks)

    fluid = produce["fluid"]
    require(fluid["ok"], "plugin fluid SDF VDB case completed", checks)
    require(fluid["source_vertex_count"] > 0, "plugin fluid SDF produced a realized mesh", checks)
    require(
        fluid["readback"]["grid_class"] == "level set" and fluid["readback"]["active_voxels"] > 0,
        "plugin fluid VDB readback is a nonempty level set",
        checks,
    )

    smoke = produce["smoke"]
    require(smoke["ok"], "plugin smoke-surface VDB case completed", checks)
    require(smoke["source_vertex_count"] > 0, "plugin smoke output produced a realized mesh", checks)
    require(
        smoke["readback"]["grid_class"] == "level set" and smoke["readback"]["active_voxels"] > 0,
        "plugin smoke VDB readback is a nonempty level set",
        checks,
    )
    require(
        smoke["result"]["native_volume_density_exported"] is False
        and "not native smoke density" in smoke["result"]["source_semantics"],
        "smoke VDB is labelled as a surface level set, not native smoke density",
        checks,
    )

    for name, result in sorted(produce["negative"].items()):
        require(result["ok"], f"negative VDB case rejected correctly: {name}", checks)

    reopen_json = vdb_dir / "reopen.json"
    run_blender(
        "openvdb_case_reopen.py",
        ["--blend", vdb_dir / "volume_import.blend", "--source", vdb_json, "--out", reopen_json],
        run_dir,
        "openvdb_reopen",
        env,
    )
    reopen = load(reopen_json)
    require(reopen["ok"], "cold reopen loads the saved Volume and re-reads the VDB", checks)
    require(reopen["volume_count"] == 1, "cold reopen has exactly one Volume object", checks)

    cache_dir = run_dir / "cache_guard"
    cache_json = cache_dir / "cache_guard.json"
    cache_dir.mkdir()
    run_blender(
        "cache_guard_case.py",
        ["--workdir", cache_dir, "--out", cache_json],
        run_dir,
        "cache_guard",
        env,
    )
    cache_result = load(cache_json)
    require(cache_result["build_ok"], "cached workflow built for the export guard test", checks)
    require(cache_result["bake"]["baked"] is True, "disk bake committed a generation", checks)
    require(
        cache_result["cached_export"]["sampling_mode"] == "CACHED_DIRECT",
        "export of a valid cache uses the committed generation directly",
        checks,
    )
    require(
        cache_result["cached_alembic"]["sampling_mode"] == "CACHED_DIRECT",
        "Alembic export of a valid cache uses the committed generation directly",
        checks,
    )
    require(cache_result["invalid_cache"]["ok"], "changed identity blocks export with CACHE_INVALID", checks)
    require(
        cache_result["invalid_alembic_cache"]["ok"],
        "changed identity blocks Alembic export with CACHE_INVALID",
        checks,
    )
    require(
        not Path(cache_dir / "invalid_surface.vdb").exists(),
        "invalid cache leaves no export artifact",
        checks,
    )

    static_dir = run_dir / "static_mesh"
    static_json = static_dir / "static.json"
    static_dir.mkdir()
    run_blender(
        "static_mesh_case.py",
        ["--workdir", static_dir, "--out", static_json],
        run_dir,
        "static_mesh",
        env,
    )
    static_result = load(static_json)
    require(static_result["ok"], "static mesh export still writes a valid blend copy and restores state", checks)

    volume_negative_dir = run_dir / "alembic_volume_negative"
    volume_negative_json = volume_negative_dir / "negative.json"
    volume_negative_dir.mkdir()
    run_blender(
        "alembic_volume_negative.py",
        ["--workdir", volume_negative_dir, "--out", volume_negative_json],
        run_dir,
        "alembic_volume_negative",
        env,
    )
    volume_negative = load(volume_negative_json)
    require(
        volume_negative["ok"],
        "volume-only Alembic target is rejected instead of producing a fake mesh",
        checks,
    )

    for case in ("rigid", "cloth"):
        case_dir = run_dir / "alembic" / case
        case_dir.mkdir(parents=True)
        source_json = case_dir / "source.json"
        run_blender(
            "alembic_case_export.py",
            ["--case", case, "--workdir", case_dir, "--out", source_json],
            run_dir,
            f"alembic_export_{case}",
            env,
        )
        source = load(source_json)
        require(source["ok"], f"Alembic {case} export completed", checks)
        require(source["abc_bytes"] > 0, f"Alembic {case} archive is nonempty", checks)
        require(
            source["export_result"]["instance_policy"] == "REALIZED_SINGLE_MESH",
            f"Alembic {case} states its realize policy",
            checks,
        )
        require(
            source["export_result"]["instance_geometry_retained"] is False
            and source["export_result"]["realized_geometry"] is True,
            f"Alembic {case} does not falsely claim instance retention",
            checks,
        )
        require(
            source["export_result"]["volume_grids_exported"] is False,
            f"Alembic {case} does not claim volume-grid export",
            checks,
        )
        restore = source["restore"]
        require(
            restore["frame"] == restore["expected_frame"]
            and restore["scene_range"] == restore["expected_scene_range"]
            and restore["selection"] == restore["expected_selection"]
            and restore["active"] == restore["expected_active"]
            and restore["modifiers"] == restore["expected_modifiers"]
            and not restore["leaked_objects"]
            and not restore["leaked_groups"],
            f"Alembic {case} restores frame/selection/active/modifiers and removes temporary objects",
            checks,
        )
        require(source["existing_path_guard"]["ok"], f"Alembic {case} refuses to overwrite an existing archive", checks)
        require(
            source["invalid_range_guard"]["ok"],
            f"Alembic {case} rejects an export range outside the managed simulation",
            checks,
        )
        require(
            source["empty_geometry_guard"]["ok"],
            f"Alembic {case} rejects empty evaluated geometry instead of writing an empty archive",
            checks,
        )
        import_json = case_dir / "import.json"
        run_blender(
            "alembic_case_import.py",
            [
                "--case", case,
                "--abc", source["abc"],
                "--source", source_json,
                "--out", import_json,
            ],
            run_dir,
            f"alembic_import_{case}",
            env,
        )
        imported = load(import_json)
        require(imported["ok"], f"Alembic {case} independent reimport matches dynamic geometry", checks)
        require(
            imported["dynamic"] and imported["dynamic"]["pass"],
            f"Alembic {case} sampled geometry actually moves across frames",
            checks,
        )
        if case == "rigid":
            require(
                all(row["pass"] for row in imported["component_checks"]),
                "realized rigid instance components map back to dynamic source instance positions",
                checks,
            )
        summary["artifacts"][case] = {
            "source": str(source_json),
            "import": str(import_json),
            "abc": source["abc"],
        }

    summary["artifacts"]["openvdb"] = {
        "produce": str(vdb_json),
        "reopen": str(reopen_json),
        "vdb": success["path"],
        "blend": success["blend"],
    }
    summary["ok"] = True
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"EXPORT_TESTS_OK run_dir={run_dir}")
    for check in checks:
        print(("PASS " if check["ok"] else "FAIL ") + check["message"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"EXPORT_TESTS_FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
