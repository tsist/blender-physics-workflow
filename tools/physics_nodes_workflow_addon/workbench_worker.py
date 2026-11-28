"""Run the installed physics core in an isolated Blender working copy."""

import importlib
import json
import os
from pathlib import Path
import sys
import traceback


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run(config, status):
    import bpy

    here = Path(__file__).resolve()
    sys.path.insert(0, str(here.parent.parent))
    addon = importlib.import_module(here.parent.name)
    if Path(addon.__file__).resolve().parent != here.parent:
        raise RuntimeError("后台进程加载的物理插件与当前安装不一致")
    addon.register()
    scene = bpy.data.scenes[config["scene"]]
    layer = scene.view_layers[config["view_layer"]]
    bpy.context.window.scene, bpy.context.window.view_layer = scene, layer
    request = config["request"]
    target = layer.objects.get(request["target_object"])
    if target is not None:
        layer.objects.active = target
        target.select_set(True)
    previous_collections = set(bpy.data.collections)
    write_json(status, {"progress": 0.24, "detail": config["detail"]})
    result = addon.execute_request(request)
    if not result.get("ok"):
        return result
    if config["save_candidate"]:
        addon.pnw_ui._theme_result(result, previous_collections)
        ui_state = scene.pnw_workbench
        ui_state.last_candidate = ""
        ui_state.last_report = "物理工作副本已生成"
        ui_state.last_report_kind = "OK"
        output = layer.objects.get(result.get("target_object", ""))
        if output is not None and not output.hide_get():
            for item in bpy.context.selected_objects:
                item.select_set(False)
            output.select_set(True)
            layer.objects.active = output
            if request.get("bridge_mode") == "MANTAFLOW_BRIDGE":
                output.pnw_settings.native_modifier_name = result["modifier_name"]
        write_json(status, {"progress": 0.9, "detail": "正在保存可编辑工作副本"})
        bpy.context.preferences.filepaths.save_version = 0
        if bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath, check_existing=False) != {"FINISHED"}:
            raise RuntimeError("无法保存模拟工作副本")
        result["candidate"] = bpy.data.filepath
    return result


def main():
    request, status, result, log = map(Path, sys.argv[sys.argv.index("--") + 1:])
    with log.open("wb") as stream:
        # The runner polls until completion. Keep verbose solver output off its pipes.
        for fd in (1, 2):
            os.dup2(stream.fileno(), fd)
        try:
            data = run(json.loads(request.read_text(encoding="utf-8")), status)
            write_json(result, data)
        except Exception as exc:
            traceback.print_exc()
            write_json(result, {"ok": False, "error": {"message": str(exc)}})


if __name__ == "__main__":
    main()
