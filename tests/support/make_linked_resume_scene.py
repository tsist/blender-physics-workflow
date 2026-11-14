"""Create a main scene with a linked collider for external-file resume tests.

The linked library is deliberately external so a test can remove it, observe a
normal workflow failure, restore the same bytes, and resume without changing
the main .blend (and therefore without changing its SHA-256).

Usage:
    blender --background --factory-startup --disable-autoexec --python-exit-code 1
        --python tests/support/make_linked_resume_scene.py -- <external.blend> <main.blend>
"""

from __future__ import annotations

from pathlib import Path
import sys

import bpy


def _args() -> list[str]:
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit("missing '-- <external.blend> <main.blend>'")
    return argv[argv.index("--") + 1 :]


def _save(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    status = bpy.ops.wm.save_as_mainfile(filepath=str(path), check_existing=False)
    if "FINISHED" not in status:
        raise SystemExit(f"save failed for {path}: {status}")


def main() -> int:
    args = _args()
    if len(args) != 2:
        raise SystemExit("expected exactly two arguments: <external.blend> <main.blend>")
    external = Path(args[0]).resolve()
    main_blend = Path(args[1]).resolve()

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.file_preview_type = "NONE"
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(4.0, 0.0, 1.0))
    collider = bpy.context.active_object
    collider.name = "ExternalCollider"
    collider.data.name = "ExternalColliderMesh"
    _save(external)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.preferences.filepaths.file_preview_type = "NONE"
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 3
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

    with bpy.data.libraries.load(str(external), link=True) as (data_from, data_to):
        if "ExternalCollider" not in data_from.objects:
            raise SystemExit(f"external collider missing from library: {data_from.objects}")
        data_to.objects = ["ExternalCollider"]
    linked = [obj for obj in data_to.objects if obj is not None]
    if not linked:
        raise SystemExit("linked collider did not load")
    for obj in linked:
        scene.collection.objects.link(obj)
    print(f"LINKED_OBJECTS={[obj.name for obj in linked]}")

    _save(main_blend)
    print(f"RESUME_LINKED_SCENE={main_blend}")
    print(f"RESUME_EXTERNAL={external}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
