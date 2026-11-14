"""Alembic sequences may begin or end empty, but must not be empty throughout.

Run with Blender --background --factory-startup --disable-autoexec
--python-exit-code 1 --python THIS_FILE -- OUTPUT_DIRECTORY.
The producer invokes a fresh Blender process to reimport the two archives.
"""
from pathlib import Path
import json
import subprocess
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from physics_nodes_workflow_addon import export as pnw_export


def counts(obj, frame):
    bpy.context.scene.frame_set(frame)
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return [len(mesh.vertices), len(mesh.polygons)]
    finally:
        evaluated.to_mesh_clear()


def state(obj):
    scene = bpy.context.scene
    return {
        "frame": scene.frame_current, "subframe": scene.frame_subframe,
        "start": scene.frame_start, "end": scene.frame_end, "step": scene.frame_step,
        "selected": sorted(ob.name for ob in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name,
        "objects": sorted(ob.name for ob in bpy.data.objects),
        "meshes": sorted(mesh.name for mesh in bpy.data.meshes),
        "groups": sorted(group.name for group in bpy.data.node_groups),
        "modifiers": [(mod.name, mod.type) for mod in obj.modifiers],
        "base_counts": [len(obj.data.vertices), len(obj.data.polygons)],
    }


def produce_case(name, out):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = name
    group = bpy.data.node_groups.new("TemporalMesh", "GeometryNodeTree")
    group.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gin, gout = group.nodes.new("NodeGroupInput"), group.nodes.new("NodeGroupOutput")
    delete = group.nodes.new("GeometryNodeDeleteGeometry")
    delete.domain = "POINT"
    group.links.new(gin.outputs["Geometry"], delete.inputs["Geometry"])
    group.links.new(delete.outputs["Geometry"], gout.inputs["Geometry"])
    if name == "always_empty":
        delete.inputs["Selection"].default_value = True
    else:
        time = group.nodes.new("GeometryNodeInputSceneTime")
        compare = group.nodes.new("ShaderNodeMath")
        compare.operation = "GREATER_THAN" if name == "end_empty" else "LESS_THAN"
        compare.inputs[1].default_value = 1.5
        group.links.new(time.outputs["Frame"], compare.inputs[0])
        group.links.new(compare.outputs[0], delete.inputs["Selection"])
    mod = obj.modifiers.new("TemporalMesh", "NODES")
    mod.node_group = group
    source = {str(frame): counts(obj, frame) for frame in (1, 2)}

    sentinel = bpy.data.objects.new("UnrelatedSelection", None)
    bpy.context.scene.collection.objects.link(sentinel)
    obj.select_set(True)
    sentinel.select_set(True)
    bpy.context.view_layer.objects.active = sentinel
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end, scene.frame_step = 7, 9, 3
    scene.frame_set(9, subframe=0.25)
    before = state(obj)
    path = out / (name + ".abc")
    request = {
        "export_format": "ALEMBIC", "export_path": str(path), "export_frame": 2,
        "parameters": {"frame_start": 1, "frame_end": 2},
    }
    result, error = None, None
    try:
        result = pnw_export.export_workflow_result(obj, request)
    except ValueError as exc:
        error = str(exc)
    after = state(obj)
    expected_export = name != "always_empty"
    checks = {
        "state_restored": before == after,
        "export_policy": bool(result and result.get("exported")) == expected_export,
        "file_policy": path.exists() == expected_export,
    }
    if expected_export:
        checks["reported_sample_nonempty"] = bool(result and result["evaluated_vertex_count"] and result["evaluated_face_count"])
        checks["reported_sample_frame"] = bool(result and result["evaluated_geometry_frame"] == (1 if name == "end_empty" else 2))
    else:
        checks["explicit_empty_error"] = bool(error and "EMPTY_EXPORT" in error)
    return {
        "passed": all(checks.values()), "checks": checks, "source_counts": source,
        "export": result, "error": error, "state_before": before, "state_after": after,
    }


def import_cases(out):
    producer = json.loads((out / "produce.json").read_text(encoding="utf-8"))
    rows = {}
    for name in ("end_empty", "start_empty"):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        path = out / (name + ".abc")
        status = sorted(bpy.ops.wm.alembic_import(filepath=str(path), as_background_job=False))
        meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        observed = {}
        for frame in (1, 2):
            frame_counts = [counts(obj, frame) for obj in meshes]
            observed[str(frame)] = [sum(row[axis] for row in frame_counts) for axis in range(2)]
        expected = producer["cases"][name]["source_counts"]
        rows[name] = {"passed": "FINISHED" in status and observed == expected,
                      "status": status, "counts": observed, "expected": expected}
    report = {"passed": all(row["passed"] for row in rows.values()), "cases": rows,
              "blender_version": bpy.app.version_string, "independent_process": True}
    (out / "import.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("ALEMBIC_EMPTY_IMPORT " + json.dumps(report))
    if not report["passed"]:
        raise SystemExit("Alembic empty-frame reimport failed")


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    if args[0] == "import":
        import_cases(Path(args[1]).resolve())
        return
    out = Path(args[0]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    cases = {name: produce_case(name, out) for name in ("end_empty", "start_empty", "always_empty")}
    report = {"passed": all(row["passed"] for row in cases.values()), "cases": cases,
              "blender_version": bpy.app.version_string, "module": pnw_export.__file__}
    (out / "produce.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report["passed"]:
        command = [bpy.app.binary_path, "--background", "--factory-startup", "--disable-autoexec",
                   "--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
                   "--", "import", str(out)]
        run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        (out / "import.log").write_text(run.stdout, encoding="utf-8")
        report["import_exit_code"] = run.returncode
        report["passed"] = run.returncode == 0
        if (out / "import.json").is_file():
            report["passed"] = report["passed"] and json.loads((out / "import.json").read_text(encoding="utf-8"))["passed"]
    (out / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("ALEMBIC_EMPTY_SEQUENCE " + json.dumps({"passed": report["passed"], "cases": {key: row["checks"] for key, row in cases.items()}, "import_exit_code": report.get("import_exit_code")}))
    if not report["passed"]:
        raise SystemExit("Alembic empty-frame sequence regression failed")


if __name__ == "__main__":
    main()
