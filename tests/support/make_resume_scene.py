"""Create a tiny deterministic Blender scene used by resume integration tests.

Usage (from Blender):
    blender --background --factory-startup --disable-autoexec --python-exit-code 1
        --python tests/support/make_resume_scene.py -- <output.blend>
"""

from __future__ import annotations

import sys

import bpy


def _args() -> list[str]:
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit("missing '-- <output.blend>'")
    return argv[argv.index("--") + 1 :]


def main() -> int:
    args = _args()
    if len(args) != 1:
        raise SystemExit("expected exactly one argument: <output.blend>")
    output = args[0]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.file_preview_type = "NONE"
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 4
    scene.render.fps = 24

    bpy.ops.mesh.primitive_grid_add(
        x_subdivisions=4,
        y_subdivisions=4,
        size=2.0,
        location=(0.0, 0.0, 2.0),
    )
    target = bpy.context.active_object
    target.name = "ResumeTarget"
    target.data.name = "ResumeTargetMesh"

    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(4.0, 0.0, 1.0))
    collider = bpy.context.active_object
    collider.name = "ResumeCollider"

    bpy.ops.object.select_all(action="DESELECT")
    target.select_set(True)
    bpy.context.view_layer.objects.active = target

    status = bpy.ops.wm.save_as_mainfile(filepath=output, check_existing=False)
    if "FINISHED" not in status:
        raise SystemExit(f"save failed: {status}")
    print(f"RESUME_TEST_SCENE={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
