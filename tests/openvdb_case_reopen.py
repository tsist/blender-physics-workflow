"""Cold-reopen a saved .blend and re-read the imported VDB with OpenVDB."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import bpy
import numpy as np
import openvdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import export_test_common as common  # noqa: E402


def main() -> None:
    args = common.parse_args()
    blend = Path(args.blend)
    produce = json.loads(Path(args.source).read_text(encoding="utf-8"))
    expected = produce["success"]
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    scene = bpy.context.scene
    volumes = [obj for obj in scene.objects if obj.type == "VOLUME"]
    rows = []
    for obj in volumes:
        data = obj.data
        data.grids.load()
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        evaluated.data.grids.load()
        path = Path(bpy.path.abspath(data.filepath))
        grids = []
        readback = None
        if path.is_file():
            loaded, metadata = openvdb.readAll(str(path))
            grid = loaded[0] if loaded else None
            if grid is not None:
                lo, hi = grid.evalActiveVoxelBoundingBox()
                shape = tuple(int(hi[i] - lo[i] + 1) for i in range(3))
                values = np.zeros(shape, np.float32)
                grid.copyToArray(values, ijk=tuple(int(v) for v in lo))
                readback = {
                    "count": len(loaded),
                    "type": type(grid).__name__,
                    "name": grid.name,
                    "grid_class": str(grid.gridClass),
                    "active_voxels": int(grid.activeVoxelCount()),
                    "world_bbox": [
                        [round(float(v), 9) for v in grid.transform.indexToWorld(tuple(int(v) for v in lo))],
                        [round(float(v), 9) for v in grid.transform.indexToWorld(tuple(int(v) for v in hi))],
                    ],
                    "finite": bool(np.isfinite(values).all()),
                    "negative_voxels": int((values < 0).sum()),
                    "positive_voxels": int((values > 0).sum()),
                    "metadata": metadata,
                }
            grids = [[item.name, item.data_type] for item in evaluated.data.grids]
        rows.append({
            "object": obj.name,
            "data_filepath": str(data.filepath),
            "resolved_path": str(path),
            "file_exists": path.is_file(),
            "grids_error": evaluated.data.grids.error_message,
            "frame_filepath": str(evaluated.data.grids.frame_filepath),
            "grids": grids,
            "readback": readback,
        })

    expected_min = produce["success"]["expected_world_min"]
    expected_max = produce["success"]["expected_world_max"]
    readbacks = [row["readback"] for row in rows if row["readback"]]
    world_ok = False
    if readbacks:
        low = readbacks[0]["world_bbox"][0]
        high = readbacks[0]["world_bbox"][1]
        world_ok = all(
            low[i] <= expected_min[i] + 0.2 and high[i] >= expected_max[i] - 0.2
            for i in range(3)
        )
    payload = {
        "ok": bool(
            len(volumes) == 1
            and rows
            and all(row["file_exists"] and not row["grids_error"] for row in rows)
            and all(row["grids"] == [["surface", "FLOAT"]] for row in rows)
            and all(
                row["readback"]
                and row["readback"]["count"] == 1
                and row["readback"]["type"] == "FloatGrid"
                and row["readback"]["name"] == "surface"
                and row["readback"]["grid_class"] == "level set"
                and row["readback"]["active_voxels"] > 0
                and row["readback"]["finite"]
                and row["readback"]["negative_voxels"] > 0
                and row["readback"]["positive_voxels"] > 0
                for row in rows
            )
            and world_ok
        ),
        "blend": str(blend),
        "volume_count": len(volumes),
        "volumes": rows,
        "expected_world_min": expected_min,
        "expected_world_max": expected_max,
        "world_bbox_ok": world_ok,
        "scene_frame": [scene.frame_current, scene.frame_subframe],
    }
    if args.out:
        common.write_json(Path(args.out), payload)
    print("OPENVDB_REOPEN_BEGIN")
    print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True))
    print("OPENVDB_REOPEN_END")


main()
