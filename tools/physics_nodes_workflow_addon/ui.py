"""Blender 3D Viewport N-Panel Workbench & Operators for Physics Nodes Workflow."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any
from uuid import uuid4

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from . import contract as pnw_contract
from . import workbench_worker
from ._ui_kit import (
    BackgroundJobRunner, HUDManager, IconPack, SidebarDock,
    apply_collection_theme, apply_node_theme, draw_brand_header,
    ensure_object_mode, get_theme, primary_button, progress_card, prop_column,
    section_panel, status_banner,
)


DOMAIN = "physics"
THEME = get_theme(DOMAIN)
_ICONS = IconPack(DOMAIN)
_RUNNER = BackgroundJobRunner(DOMAIN)
_UI_PROPERTY = "pnw_workbench"
_PENDING_PARAMETERS = {}
_EXPORT_SNAPSHOT = None
_JOB_SCENE = None

_BOX_TIPPING_PRESET = "rigid_box_tipping"
_NATIVE_DOMAIN_PRESETS = {domain: preset for preset, domain in pnw_contract.NATIVE_FLUID_PRESETS.items()}
_NATIVE_PATH_OPTIONS = {"OUTPUT_PATH", "PATH_SUPPORTS_BLEND_RELATIVE"} if bpy.app.version >= (5, 3, 0) else set()
_NATIVE_ACTIONS = (
    ("adopt", "接管流体域", "将本地流体域交给物理工作台管理"),
    ("bake", "烘焙流体缓存", "将结果写入新的受管缓存代次"),
    ("inspect", "检查流体域", "检查求解参数与缓存"),
    ("invalidate_cache", "解除缓存绑定", "解除当前缓存关联并保留磁盘文件"),
    ("export", "导出液体定格", "将指定帧的液面保存为独立网格"),
    ("guide_density", "创建或定位密度导向", "在当前烟气域创建或查找可编辑的导向对象"),
    ("export_density", "导出密度序列", "使用当前导向设置导出逐帧 VDB"),
)

_PRESETS = {
    "rigid_drop_pack": ("刚体散落", "实例散落、反弹、摩擦与碰撞"),
    "rigid_chain_pendulum": ("刚体链摆", "使用锚点与距离约束模拟链条摆动"),
    "rigid_box_tipping": ("箱体倾倒", "在固定水平地面模拟单轴箱体倾倒"),
    "cloth_banner_wind": ("迎风布料", "带固定点、风场和碰撞的布料或窄带"),
    "rope_catenary_swing": ("悬绳摆动", "绳索受到重力、风摆和碰撞"),
    "softbody_elastic_shell": ("弹性软壳", "模拟闭合三角曲面的弹性与弯曲"),
    "fluid_fountain_sdf": ("喷泉与液面", "从喷射粒子重建可导出的液面"),
    "smoke_plume_volume": ("上升烟羽", "用浮力、湍流和体积实现烟羽"),
}


def _preset_items(self, context):
    return [(pid, *_PRESETS[pid]) for pid in pnw_contract.PRESET_DEFINITIONS if pid in _PRESETS]


def _workspace(scene):
    return getattr(scene, _UI_PROPERTY)


def _status(scene, message, kind="INFO"):
    state = _workspace(scene)
    state.last_report = message
    state.last_report_kind = kind


def _managed_modifier(obj):
    if obj is None or not hasattr(obj, "pnw_settings"):
        return None
    mod = obj.modifiers.get(obj.pnw_settings.modifier_name or "PhysicsNodes")
    if mod is not None and mod.type == "NODES" and mod.node_group and mod.node_group.get("pnw_role") == "wrapper":
        return mod
    return None


def _theme_result(result, previous_collections):
    workflow = result.get("workflow") or {}
    visited = set()

    def tint_tree(tree):
        if tree is None or tree in visited or tree.library or tree.users > 1:
            return
        visited.add(tree)
        for node in tree.nodes:
            apply_node_theme(node, DOMAIN)
            tint_tree(getattr(node, "node_tree", None))

    wrapper_name = workflow.get("wrapper_name")
    if wrapper_name:
        tint_tree(bpy.data.node_groups.get(wrapper_name))
    for collection in set(bpy.data.collections) - previous_collections:
        apply_collection_theme(collection, DOMAIN)


def _publish_result(context, result, operation):
    if not result.get("ok"):
        error = result.get("error") or {}
        code = error.get("code")
        reason = {
            "VALIDATION_ERROR": "请核对目标、帧范围和模拟参数",
            "NATIVE_FLUID_CACHE_REQUIRED": "请先烘焙并打开含受管缓存的副本",
            "NATIVE_FLUID_CACHE_STALE": "流体设置或输入已变更，请重新烘焙",
            "NATIVE_FLUID_WORK_COPY": "请先保存本地工作副本",
            "NATIVE_FLUID_EXTERNAL_CACHE": "请使用受管缓存或新建流体域",
        }.get(code, "")
        _status(context.scene, reason or f"物理操作失败：{error.get('message') or '请检查当前设置'}", "ERROR")
        return
    inspection = result.get("inspection") or {}
    population = (inspection.get("frames") or {}).get(str((inspection.get("frame_range") or [0])[-1]), {})
    contacts = (population.get("ground_contact") or {}).get("visible_geometry") or {}
    check = inspection.get("diagnostics_valid")
    if operation == "inspect":
        if "cache_state" in inspection:
            cache = inspection["cache_state"]
            message = {
                "valid": "流体缓存校验通过", "fresh": "当前流体域尚未烘焙",
                "unbaked": "当前流体域尚未烘焙", "invalid": "当前流体缓存需要重新烘焙",
            }.get(cache, "已检查流体域和缓存")
        else:
            message = {True: "模拟诊断通过", False: "当前帧诊断数据不足"}.get(check, "已检查物理工作流")
            if contacts.get("clearance_min") is not None:
                message += f"。地面最小距离 {contacts['clearance_min']:.3f} 米"
    else:
        message = {
            "build": "物理工作流已构建", "parameters": "已更新当前模拟参数",
            "bake": "模拟缓存已完成", "invalidate_cache": "已解除缓存绑定，保留磁盘文件",
            "prepare_instances": "已整理独立实例集合", "export": "已导出物理结果",
            "export_density": "已导出密度序列", "guide_density": "密度导向已就绪",
            "adopt": "已接管流体域",
        }.get(operation, "物理工作流已更新")
    _status(context.scene, message, "WARN" if check is False and operation == "inspect" else "OK")


def _flush_live_parameters():
    from . import core

    params = tuple(_PENDING_PARAMETERS.items())
    _PENDING_PARAMETERS.clear()
    scene = bpy.context.scene
    for uid, object_name in params:
        obj = scene.objects.get(object_name) if scene else None
        mod = _managed_modifier(obj)
        if mod is None or obj.session_uid != uid or obj.mode != "OBJECT":
            continue
        settings = obj.pnw_settings
        request = _inspection_request(obj, settings.to_request(obj, "parameters"))
        try:
            request = pnw_contract.normalize_request(request)
            built = json.loads(obj.get(f"pnw_state_json__{mod.name}", "{}"))
            if built.get("preset_id") != request["preset_id"] or built.get("parameters_hash") == request["parameters_hash"]:
                continue
            core.build_or_update_workflow(obj, request)
            _status(scene, "已实时更新模拟参数", "OK")
        except (ValueError, RuntimeError) as exc:
            _status(scene, f"参数无法实时应用：{exc}", "WARN")
    return None


def _schedule_live_parameters(settings, context):
    if bpy.app.background or context is None or not settings.live_parameters:
        return
    obj = settings.id_data
    if not isinstance(obj, bpy.types.Object) or _managed_modifier(obj) is None:
        return
    _PENDING_PARAMETERS[obj.session_uid] = obj.name
    if bpy.app.timers.is_registered(_flush_live_parameters):
        bpy.app.timers.unregister(_flush_live_parameters)
    bpy.app.timers.register(_flush_live_parameters, first_interval=0.3)


def _release_job():
    global _EXPORT_SNAPSHOT, _JOB_SCENE
    if _EXPORT_SNAPSHOT is not None:
        _EXPORT_SNAPSHOT.cleanup()
        _EXPORT_SNAPSHOT = None
    _JOB_SCENE = None


@persistent
def _cleanup_on_load(_unused):
    _RUNNER.cleanup()
    _release_job()
    _PENDING_PARAMETERS.clear()
    if bpy.app.timers.is_registered(_flush_live_parameters):
        bpy.app.timers.unregister(_flush_live_parameters)


def _mesh_filter(_settings, obj):
    return obj.type == "MESH"


class PNW_WorkbenchSettings(bpy.types.PropertyGroup):
    output_directory: StringProperty(name="工作副本位置", subtype="DIR_PATH")
    last_candidate: StringProperty(subtype="FILE_PATH")
    last_report: StringProperty()
    last_report_kind: StringProperty(default="INFO")
    fracture_points: IntProperty(name="破碎种子点", min=2, max=1024, default=32)
    cloth_pin_group: StringProperty(name="固定顶点组")
    native_domain_type: EnumProperty(name="流体类型", items=(
        ("native_liquid", "液体", "创建原生液体域"),
        ("native_gas", "烟气", "创建原生烟气域"),
    ))
    native_flow: PointerProperty(name="发射网格", type=bpy.types.Object, poll=_mesh_filter)
    native_collider: PointerProperty(name="碰撞网格", type=bpy.types.Object, poll=_mesh_filter)
    native_margin: FloatProperty(name="域边界余量", min=0.01, max=20, default=0.8, subtype="DISTANCE")
    native_resolution: IntProperty(name="流体分辨率", min=16, max=256, default=32)


class PNW_ObjectSettings(bpy.types.PropertyGroup):
    preset_id: EnumProperty(
        name="物理配方",
        description="选择所用物理配方",
        items=_preset_items,
    )
    bridge_mode: EnumProperty(
        name="求解器",
        description="选择受管几何节点求解或 Blender 原生模拟",
        items=[
            ("GN_NATIVE", "几何节点求解", "使用几何节点模拟区和迭代子步"),
            ("XPBD_ASSET_BRIDGE", "XPBD 兼容模式", "兼容旧工程设置，布料配方使用内置 XPBD"),
            ("BULLET_BRIDGE", "刚体兼容模式", "兼容旧工程设置，使用刚体分区创建原生刚体"),
            ("MANTAFLOW_BRIDGE", "原生流体", "在原生液体与烟气分区管理流体域"),
        ],
        default="GN_NATIVE",
    )
    modifier_name: StringProperty(name="修改器名称", default="PhysicsNodes")
    collider_object: PointerProperty(name="碰撞物体", type=bpy.types.Object, update=_schedule_live_parameters)
    collider_collection: PointerProperty(name="碰撞集合", type=bpy.types.Collection, update=_schedule_live_parameters)
    instance_collection: PointerProperty(name="实例集合", type=bpy.types.Collection, update=_schedule_live_parameters)
    instance_normalize: BoolProperty(name="按代理半径调整实例", default=True, update=_schedule_live_parameters)
    protect_source: BoolProperty(name="保护原件并创建工作副本", default=True,
        description="首次构建创建独立副本，后续更新该副本")

    live_parameters: BoolProperty(name="实时应用参数", default=True)

    substeps: IntProperty(name="子步数", default=4, min=1, max=8, update=_schedule_live_parameters)
    contact_iterations: IntProperty(name="接触迭代次数", default=3, min=1, max=32, update=_schedule_live_parameters)
    constraint_iterations: IntProperty(name="约束迭代次数", default=12, min=1, max=64, update=_schedule_live_parameters)
    seed: IntProperty(name="随机种子", default=42, min=0, update=_schedule_live_parameters)
    frame_start: IntProperty(name="起始帧", default=1, min=1)
    frame_end: IntProperty(name="结束帧", default=120, min=1)
    activation_frame: IntProperty(
        name="激活帧", default=0, min=0, max=100000,
        description="整组刚体开始运动的场景帧。设为 0 从起始帧激活",
    )

    gravity: FloatVectorProperty(name="重力", default=(0.0, 0.0, -9.81), size=3, update=_schedule_live_parameters)
    drag: FloatProperty(name="阻尼", default=0.04, min=0.0, max=20.0, update=_schedule_live_parameters)
    restitution: FloatProperty(name="反弹系数", default=0.35, min=0.0, max=1.0, update=_schedule_live_parameters)
    friction: FloatProperty(name="摩擦系数", default=0.45, min=0.0, max=1.0, update=_schedule_live_parameters)
    sleep_velocity: FloatProperty(name="休眠速度阈值", default=0.05, min=0.0, max=10.0, update=_schedule_live_parameters)
    contact_thickness: FloatProperty(name="碰撞厚度", default=0.08, min=0.001, max=2.0, update=_schedule_live_parameters)
    particle_radius: FloatProperty(name="代理半径", default=0.12, min=0.001, max=5.0, update=_schedule_live_parameters)
    ground_z: FloatProperty(name="地面高度", default=0.0, update=_schedule_live_parameters)
    box_half_extents: FloatVectorProperty(
        name="箱体半尺寸",
        description="箱体三个轴向的半尺寸，均为正数",
        default=(0.2, 0.3, 1.0),
        size=3,
        min=1e-6,
    )
    box_initial_tilt: FloatProperty(
        name="箱体初始倾角",
        description="围绕 Y 轴的初始旋转角",
        subtype="ANGLE",
        default=0.3,
        min=-math.pi / 2.0 + 1e-6,
        max=math.pi / 2.0 - 1e-6,
    )

    scatter_count: IntProperty(name="散落数量", default=48, min=0, max=10000, update=_schedule_live_parameters)
    angular_spin: FloatProperty(name="初始旋转强度", default=1.2, min=0.0, max=50.0, update=_schedule_live_parameters)
    stiffness: FloatProperty(name="刚度", default=0.7, min=0.0, max=1.0, update=_schedule_live_parameters)
    bending: BoolProperty(name="布料弯曲约束", default=False,
        description="为网格面添加跨边弯曲约束")
    bend_softness: FloatProperty(name="弯曲柔度", default=0.1, min=0.0, max=1.0, update=_schedule_live_parameters)
    tearing: BoolProperty(name="允许布料撕裂", default=False,
        description="相对拉伸超过阈值时断开网格边")
    tear_threshold: FloatProperty(name="撕裂应变阈值", default=1.2, min=1.0, max=100.0, update=_schedule_live_parameters)
    anchor_pos: FloatVectorProperty(name="世界空间锚点", default=(0.0, 0.0, 2.5), size=3, update=_schedule_live_parameters)
    rest_length: FloatProperty(name="静止长度", default=0.0, min=0.0, update=_schedule_live_parameters)
    curve_resolution: IntProperty(name="曲线采样数", default=64, min=2, max=50000, update=_schedule_live_parameters)

    wind_vector: FloatVectorProperty(name="风力向量", default=(0.0, 0.0, 0.0), size=3, update=_schedule_live_parameters)
    wind_turbulence: FloatProperty(name="风力湍流", default=0.0, min=0.0, max=50.0, update=_schedule_live_parameters)

    emit_rate: IntProperty(name="喷射速率", default=24, min=0, max=5000, update=_schedule_live_parameters)
    emit_speed: FloatProperty(name="初始喷射速度", default=6.0, min=0.0, max=100.0, update=_schedule_live_parameters)
    emit_end_frame: IntProperty(name="喷射结束帧", default=90, min=1, update=_schedule_live_parameters)
    lifetime: IntProperty(name="生命周期（帧）", default=110, min=1, update=_schedule_live_parameters)
    buoyancy: FloatProperty(name="浮力", default=0.0, min=-50.0, max=50.0, update=_schedule_live_parameters)
    sdf_voxel_size: FloatProperty(name="SDF 体素尺寸", default=0.08, min=0.01, max=2.0, update=_schedule_live_parameters)

    bake_directory: StringProperty(name="缓存目录", subtype="DIR_PATH", default="")
    export_format: EnumProperty(name="交付格式", items=[
        ("STATIC_MESH", "静态网格（.blend）", "将指定帧固化为独立的网格副本"),
        ("ALEMBIC", "Alembic 动画（.abc）", "导出当前帧范围"),
        ("OPENVDB", "OpenVDB 网格（.vdb）", "将求值表面转为 VDB 体积网格")], default="STATIC_MESH")
    export_path: StringProperty(name="输出文件", subtype="FILE_PATH", default="")
    export_frame: IntProperty(name="导出帧", default=1, min=1)
    last_inspect_summary: StringProperty(name="最近诊断", default="")
    last_export_summary: StringProperty(name="最近导出", default="")
    last_instance_summary: StringProperty(name="实例整理回执", default="")
    native_modifier_name: StringProperty(name="流体修改器", default="")
    native_cache_root: StringProperty(
        name="受管流体缓存目录", subtype="DIR_PATH", options=_NATIVE_PATH_OPTIONS, default="",
        description="工作台生成的独立缓存存放位置",
    )
    native_export_path: StringProperty(
        name="液面输出文件", subtype="FILE_PATH", options=_NATIVE_PATH_OPTIONS, default="",
    )
    native_export_frame: IntProperty(
        name="导出帧（0 为当前帧）", min=0, default=0,
        description="选择当前帧或输入已烘焙范围内的帧",
    )
    native_guide_name: StringProperty(name="烟气密度导向", default="")
    native_bypass: PointerProperty(name="绕行控制物体", type=bpy.types.Object)
    native_exit: PointerProperty(name="出口控制物体", type=bpy.types.Object)
    native_density_output: StringProperty(
        name="VDB 输出目录", subtype="DIR_PATH", options=_NATIVE_PATH_OPTIONS, default="",
    )
    native_last_summary: StringProperty(name="最近的流体操作", default="")

    def to_request(self, obj: bpy.types.Object, operation: str, bake: bool = False) -> dict[str, Any]:
        parameters = {
            "instance_normalize": bool(self.instance_normalize),
            "substeps": int(self.substeps),
            "contact_iterations": int(self.contact_iterations),
            "constraint_iterations": int(self.constraint_iterations),
            "seed": int(self.seed),
            "frame_start": int(self.frame_start),
            "frame_end": int(self.frame_end),
            "activation_frame": int(self.activation_frame),
            "gravity": list(self.gravity),
            "drag": float(self.drag),
            "restitution": float(self.restitution),
            "friction": float(self.friction),
            "sleep_velocity": float(self.sleep_velocity),
            "contact_thickness": float(self.contact_thickness),
            "particle_radius": float(self.particle_radius),
            "ground_z": float(self.ground_z),
            "scatter_count": int(self.scatter_count),
            "angular_spin": float(self.angular_spin),
            "stiffness": float(self.stiffness),
            "bending": bool(self.bending),
            "bend_softness": float(self.bend_softness),
            "tearing": bool(self.tearing),
            "tear_threshold": float(self.tear_threshold),
            "anchor_pos": list(self.anchor_pos),
            "rest_length": float(self.rest_length),
            "curve_resolution": int(self.curve_resolution),
            "wind_vector": list(self.wind_vector),
            "wind_turbulence": float(self.wind_turbulence),
            "emit_rate": int(self.emit_rate),
            "emit_speed": float(self.emit_speed),
            "emit_end_frame": int(self.emit_end_frame),
            "lifetime": int(self.lifetime),
            "buoyancy": float(self.buoyancy),
            "sdf_voxel_size": float(self.sdf_voxel_size),
        }
        request = {
            "schema_version": "1.0",
            "operation": operation,
            "preset_id": self.preset_id,
            "bridge_mode": self.bridge_mode,
            "target_object": obj.name,
            "modifier_name": self.modifier_name or "PhysicsNodes",
            "collider_object": self.collider_object.name if self.collider_object else None,
            "collider_collection": self.collider_collection.name if self.collider_collection else None,
            "instance_collection": self.instance_collection.name if self.instance_collection else None,
            "bake": bake,
            "bake_directory": self.bake_directory or None,
            "export_format": self.export_format if operation == "export" else "NONE",
            "export_path": bpy.path.abspath(self.export_path) if operation == "export" and self.export_path else None,
            "export_frame": self.export_frame if operation == "export" and self.export_format != "ALEMBIC" else self.frame_end,
            "parameters": parameters,
        }
        if self.preset_id == _BOX_TIPPING_PRESET:
            parameters["box_half_extents"] = [float(value) for value in self.box_half_extents]
            parameters["box_initial_tilt"] = float(self.box_initial_tilt)
            # Hidden controls from a previous preset must not create an
            # unfixable request. Keep their UI values for switching back.
            request["bridge_mode"] = "GN_NATIVE"
            for name in ("collider_object", "collider_collection", "instance_collection"):
                request[name] = None
            parameters["scatter_count"] = 0
            parameters["gravity"][1] = 0.0
        return request


def _inspection_request(obj, request):
    """Recover exact persisted values after a lossless Blender UI round trip.

    FloatProperty stores float32. Merely displaying a CLI value such as -9.81
    must not invalidate its cache. Only restore values whose complete request
    still matches the stored identity; real edits and changed references remain
    subject to the usual strict cache validation.
    """
    import json
    import struct
    from . import contract

    def ui_value(value):
        if isinstance(value, float):
            return struct.unpack('f', struct.pack('f', value))[0]
        if isinstance(value, list):
            return [ui_value(item) for item in value]
        if isinstance(value, dict):
            return {key: ui_value(item) for key,item in value.items()}
        return value

    try:
        state = json.loads(obj.get(f"pnw_state_json__{request['modifier_name']}", "{}"))
        stored = state.get("parameters")
        current = contract.normalize_request(request)
        if not isinstance(stored, dict) or ui_value(stored) != ui_value(current['parameters']):
            return request
        restored = dict(request, parameters=stored)
        if contract.normalize_request(restored)['parameters_hash'] == state.get('parameters_hash'):
            return restored
    except (ValueError, TypeError, KeyError, OverflowError):
        pass
    return request


def _run_op(operator, context, operation: str, bake: bool = False, force_rebuild: bool = False):
    from . import execute_request

    obj = context.active_object
    if obj is None:
        operator.report({"ERROR"}, "请先选择活动目标物体")
        return {"CANCELLED"}
    req = obj.pnw_settings.to_request(obj, operation, bake=bake)
    if operation == "inspect":
        req = _inspection_request(obj, req)
    req["force_rebuild"] = force_rebuild
    previous_collections = set(bpy.data.collections)
    with ensure_object_mode(context):
        res = execute_request(req)
    _publish_result(context, res, operation)
    if not res.get("ok"):
        obj.pnw_settings.last_inspect_summary = "Diagnostics unavailable: " + res.get("error", {}).get("message", "Execution failed")
        operator.report({"ERROR"}, _workspace(context.scene).last_report)
        return {"CANCELLED"}
    if operation in {"build", "parameters"}:
        _theme_result(res, previous_collections)
    if "inspection" in res and res["inspection"]:
        insp = res["inspection"]
        valid = insp.get("diagnostics_valid") is True
        coverage = insp.get("state_coverage") or {}
        velocity = {"evaluated_output_pnw_velocity": "Output velocity",
                    "estimated_from_sampled_position_delta": "Estimated from positions"}.get(
                        insp.get("speed_source"), "Unavailable")
        speed = insp.get('mean_speed')
        low_speed = insp.get('resting_ratio')
        lines = ["Diagnostics valid" if valid else "Diagnostics invalid; inspect evidence",
                 "Output physics attributes: " + ("available" if coverage.get("physics_attributes_compared") else "unavailable"),
                 f"Speed: {speed if speed is not None else 'unavailable'} ({velocity})",
                 f"Low-speed fraction: {low_speed if low_speed is not None else 'unavailable'}",
                 f"Displacement: {insp['centroid_displacement'] if insp.get('centroid_displacement') is not None else 'unavailable'}"]
        if insp.get('speed_source') == 'not_observable':
            reason = (insp.get('velocity_attribute_status') if insp.get('velocity_attribute_status') in ('invalid_attribute', 'partial')
                      else insp.get('late_motion', {}).get('reason'))
            lines.append("Motion unavailable: " + str(reason or 'insufficient evidence'))
        end_frame = str(insp.get("frame_range", [0, 0])[-1])
        end = insp.get("frames", {}).get(end_frame, {})
        population = end.get("particle_population", {})
        if population.get("status") == "observed":
            lines.append(f"Simulated particles: {population['count']}")
        ground = end.get('ground_contact', {}).get('visible_geometry', {})
        if ground.get('status') == 'observed':
            lines.append(f"Mesh to ground plane: {ground['clearance_min']:.6g}")
            lines.append(f"Vertices below plane: {ground['below_ground_vertex_count']}")
        else:
            lines.append("Mesh to ground plane: " + ground.get('status', 'unavailable'))
        sleep = end.get('solver_sleep', {})
        if sleep.get('status') == 'observed':
            lines.append(f"Solver sleeping: {sleep['sleeping_count']}/{sleep['count']} output points")
        else:
            lines.append("Solver sleeping: " + sleep.get('status', 'unavailable'))
        contact = end.get("contact_convergence", {})
        if contact.get("status") == "observed":
            state = "converged" if contact["converged"] else "NOT converged"
            lines.append(f"Sphere contact: {state}, overlap {contact['max_residual']:.6g}")
        else:
            lines.append("Sphere contact: " + contact.get('status', 'unavailable'))
        obj.pnw_settings.last_inspect_summary = "\n".join(lines)
        if not valid:
            operator.report({"WARNING"}, "当前帧缺少足够的模拟诊断数据")
    exported = res.get("export")
    if isinstance(exported, dict) and exported.get("exported"):
        summaries = {
            "STATIC_MESH": ["Static mesh exported", "Material assignments and shaders retained",
                            "External images remain at original paths", "Other scene dependencies are not checked"],
            "ALEMBIC": ["Animated mesh exported", "Material names and UV maps retained",
                        "Shaders and images are not included", "Choose render UV after importing"],
            "OPENVDB": ["Surface level set exported", "No materials, UVs or smoke density included"],
        }
        lines = summaries.get(exported.get("export_format"), ["Export completed; inspect delivery details"])
        obj.pnw_settings.last_export_summary = "\n".join(lines)
    operator.report({"INFO"}, _workspace(context.scene).last_report)
    return {"FINISHED"}


class PNW_OT_load_preset_defaults(bpy.types.Operator):
    bl_idname = "pnw.load_preset_defaults"
    bl_label = "载入配方默认值"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            return {"CANCELLED"}
        s = obj.pnw_settings
        defaults = pnw_contract.PRESET_DEFINITIONS[s.preset_id]["defaults"]
        for k, v in defaults.items():
            if hasattr(s, k):
                setattr(s, k, v)
        _status(context.scene, "已载入当前配方的默认参数", "OK")
        return {"FINISHED"}


class PNW_OT_build_workflow(bpy.types.Operator):
    bl_idname = "pnw.build_workflow"
    bl_label = "构建或更新物理工作流"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type in {"MESH", "CURVE", "CURVES"}

    def execute(self, context):
        obj = context.active_object
        if obj is not None and obj.pnw_settings.protect_source and not obj.get("pnw_working_copy_source"):
            mod = obj.modifiers.get(obj.pnw_settings.modifier_name)
            managed = mod is not None and mod.type == "NODES" and mod.node_group and mod.node_group.get("pnw_role") == "wrapper"
            if not managed:
                return bpy.ops.pnw.create_working_copy()
        return _run_op(self, context, "build")


class PNW_OT_create_working_copy(bpy.types.Operator):
    bl_idname = "pnw.create_working_copy"
    bl_label = "创建物理工作副本"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type in {"MESH", "CURVE", "CURVES"}

    def execute(self, context):
        source = context.active_object
        with ensure_object_mode(context):
            working = source.copy()
            working.data = source.data.copy()
            working.name = source.name + "_Physics"
            working["pnw_working_copy_source"] = source.name_full
            root = context.scene.collection
            collection = next((item for item in root.children
                               if item.get("pnw_gui_collection") and item.users == 1 and not item.library), None)
            if collection is None:
                collection = bpy.data.collections.new("受管物理")
                collection["pnw_gui_collection"] = True
                root.children.link(collection)
            collection.objects.link(working)
            apply_collection_theme(collection, DOMAIN)
            context.view_layer.update()
            for item in context.selected_objects:
                item.select_set(False)
            working.select_set(True)
            context.view_layer.objects.active = working
            result = _run_op(self, context, "build")
            if result != {"FINISHED"}:
                self.report({"WARNING"}, "已保留工作副本，请查看构建回执")
        return result


class PNW_OT_prepare_instances(bpy.types.Operator):
    bl_idname = "pnw.prepare_instances"
    bl_label = "整理实例副本"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.library is None and obj.pnw_settings.instance_collection is not None

    def execute(self, context):
        from . import execute_request
        obj = context.active_object
        previous_collections = set(bpy.data.collections)
        with ensure_object_mode(context):
            result = execute_request(obj.pnw_settings.to_request(obj, "prepare_instances"))
        _publish_result(context, result, "prepare_instances")
        if not result.get("ok"):
            self.report({"ERROR"}, _workspace(context.scene).last_report)
            return {"CANCELLED"}
        _theme_result(result, previous_collections)
        prepared = result["prepared_instances"]
        obj.pnw_settings.instance_collection = bpy.data.collections[prepared["prepared_collection"]]
        obj.pnw_settings.last_instance_summary = (f"Prepared {len(prepared['objects'])} independent mesh copies.\n"
            "Source stays unchanged; shaders remain shared.\nBuild / Update to use the selected copies.")
        self.report({"INFO"}, "已整理实例，构建或更新后即可使用")
        return {"FINISHED"}


class PNW_OT_rebuild_workflow(bpy.types.Operator):
    bl_idname = "pnw.rebuild_workflow"
    bl_label = "重建并保留旧节点图"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return _managed_modifier(obj) is not None

    def execute(self, context):
        return _run_op(self, context, "build", force_rebuild=True)


class PNW_OT_export_result(bpy.types.Operator):
    bl_idname = "pnw.export_result"
    bl_label = "导出物理结果"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if context.active_object is None or not context.active_object.pnw_settings.export_path:
            self.report({"ERROR"}, "请选择新的输出文件路径")
            return {"CANCELLED"}
        return _run_op(self, context, "export")


class PNW_OT_pin_selection(bpy.types.Operator):
    bl_idname = "pnw.pin_selection"
    bl_label = "固定所选顶点"
    bl_options = {"REGISTER", "UNDO"}
    unpin: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        import bmesh
        obj = context.active_object
        if obj.library or obj.data.library:
            self.report({"ERROR"}, "请先将网格复制到本地工作副本")
            return {"CANCELLED"}
        value = 0.0 if self.unpin else 1.0
        if obj.mode == "EDIT":
            if obj.data.users > 1:
                self.report({"ERROR"}, "请先将共享网格独立复制，再编辑固定点")
                return {"CANCELLED"}
            bm = bmesh.from_edit_mesh(obj.data)
            layer = bm.verts.layers.float.get("pnw_pinned") or bm.verts.layers.float.new("pnw_pinned")
            for v in bm.verts:
                if v.select:
                    v[layer] = value
            bmesh.update_edit_mesh(obj.data)
        else:
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            attr = obj.data.attributes.get("pnw_pinned")
            if attr and (attr.domain != "POINT" or attr.data_type != "FLOAT"):
                self.report({"ERROR"}, "固定权重必须使用点域上的浮点属性")
                return {"CANCELLED"}
            attr = attr or obj.data.attributes.new("pnw_pinned", "FLOAT", "POINT")
            for v in obj.data.vertices:
                if v.select:
                    attr.data[v.index].value = value
            obj.data.update()
        _status(context.scene, "已解除所选顶点的固定" if self.unpin else "已固定所选顶点", "OK")
        return {"FINISHED"}


class PNW_OT_update_parameters(bpy.types.Operator):
    bl_idname = "pnw.update_parameters"
    bl_label = "应用参数并保留节点编辑"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _run_op(self, context, "parameters")


class PNW_OT_bake_cache(bpy.types.Operator):
    bl_idname = "pnw.bake_cache"
    bl_label = "烘焙到磁盘"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "bake", bake=True)


class PNW_OT_invalidate_cache(bpy.types.Operator):
    bl_idname = "pnw.invalidate_cache"
    bl_label = "解除模拟缓存"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "invalidate_cache")


class PNW_OT_inspect_workflow(bpy.types.Operator):
    bl_idname = "pnw.inspect_workflow"
    bl_label = "检查模拟"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "inspect")


def _fluid_modifiers(obj):
    return [mod for mod in obj.modifiers if mod.type == "FLUID"] if obj is not None else []


def _domain_modifier(obj):
    selected = obj.pnw_settings.native_modifier_name
    mods = [mod for mod in _fluid_modifiers(obj)
            if mod.fluid_type == "DOMAIN" and mod.domain_settings is not None]
    if selected:
        return next((mod for mod in mods if mod.name == selected), None)
    return mods[0] if len(mods) == 1 else None


def _native_path(value):
    if not value:
        return None
    if not bpy.data.filepath and value.startswith("//"):
        raise ValueError("请保存工作副本后再使用相对路径")
    path = bpy.path.abspath(value)
    if os.path.isabs(path):
        return os.path.normpath(path)
    if not bpy.data.filepath:
        raise ValueError("请保存工作副本或选择绝对路径")
    return os.path.normpath(os.path.join(os.path.dirname(bpy.data.filepath), path))


def _background_request(context, obj, action, root):
    settings = obj.pnw_settings
    workbench = _workspace(context.scene)
    if action in {"bake", "export"}:
        request = settings.to_request(obj, action, bake=action == "bake")
        if action == "bake":
            request["bake_directory"] = _native_path(settings.bake_directory) or str(root / "cache")
        elif not request["export_path"]:
            raise ValueError("请选择交付文件位置")
        return request

    if action in {"native_bake", "native_export", "density_export"}:
        modifier = _domain_modifier(obj)
        if modifier is None:
            raise ValueError("请先选择有效的流体域")
        operation = {"native_bake": "bake", "native_export": "export", "density_export": "export_density"}[action]
        request = _native_request(obj, modifier, operation, context.scene.frame_current)
        if action == "native_bake":
            request.setdefault("bake_directory", str(root / "cache"))
        return request

    frame_range = {"frame_start": settings.frame_start, "frame_end": settings.frame_end}
    request = {"schema_version": "1.0", "target_object": obj.name,
               "operation": "bake", "parameters": frame_range, "bake_directory": str(root / "cache")}
    if action == "fracture":
        if obj.type != "MESH":
            raise ValueError("请选中带非退化表面的闭合网格")
        collider = settings.collider_object
        if collider is obj:
            raise ValueError("请为破碎刚体选择独立的地面或碰撞网格")
        bodies = [{"object": collider.name, "type": "PASSIVE", "collision_shape": "MESH"}] if collider else []
        request.update(preset_id="native_rigid_body", native_setup={
            "bodies": bodies, "fracture": {
                "object": obj.name, "source_limit": workbench.fracture_points, "seed": settings.seed,
            },
        })
        return request
    if action == "cloth":
        if obj.type != "MESH":
            raise ValueError("请选择布料网格")
        layer = {"object": obj.name, "quality": settings.substeps,
                 "tension_stiffness": 1 + 39 * settings.stiffness,
                 "bending_stiffness": max(0.0, (1 - settings.bend_softness) * 2),
                 "distance_min": settings.contact_thickness}
        if workbench.cloth_pin_group:
            layer["pin_group"] = workbench.cloth_pin_group
        collider = settings.collider_object
        if collider is obj:
            raise ValueError("布料与碰撞物体必须分开")
        request.update(preset_id="native_cloth", native_setup={
            "layers": [layer], "colliders": [collider.name] if collider else [],
        })
        return request

    flow = workbench.native_flow or obj
    if flow.type != "MESH" or context.view_layer.objects.get(flow.name) != flow:
        raise ValueError("请选择当前视图层的发射网格")
    collider = workbench.native_collider
    if collider is flow:
        raise ValueError("发射网格和碰撞网格必须分开")
    title = "受管液体域" if workbench.native_domain_type == "native_liquid" else "受管烟气域"
    request.update(operation="build", bridge_mode="MANTAFLOW_BRIDGE", preset_id=workbench.native_domain_type,
                   target_object=f"{title} {uuid4().hex[:6]}", modifier_name=title,
                   native_setup={"inflows": [flow.name], "effectors": [collider.name] if collider else [],
                                 "domain_margin": workbench.native_margin, "cache_type": "MODULAR"},
                   native_settings={"resolution_max": workbench.native_resolution})
    return request


_JOBS = (
    ("bake", "烘焙物理缓存", "在独立工作副本中烘焙并验证模拟缓存"),
    ("export", "导出物理结果", "按选定格式输出当前模拟的结果"),
    ("fracture", "生成并烘焙破碎刚体", "使用 Cell Fracture 分块并保存原生刚体动画"),
    ("cloth", "烘焙原生布料", "按固定组和碰撞体保存原生布料工作副本"),
    ("native_create", "创建原生流体域", "按发射网格和碰撞体创建可编辑流体域"),
    ("native_bake", "烘焙原生流体", "在独立工作副本中运行原生流体求解"),
    ("native_export", "导出液体定格", "将已烘焙液面保存为独立网格"),
    ("density_export", "导出烟气密度", "将已烘焙的烟气与导向导出为 OpenVDB 序列"),
)


class PNW_OT_background_job(bpy.types.Operator):
    bl_idname = "pnw.background_job"
    bl_label = "运行后台物理任务"
    action: EnumProperty(items=_JOBS)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type in {"MESH", "CURVE", "CURVES"} and obj.library is None

    def execute(self, context):
        global _EXPORT_SNAPSHOT, _JOB_SCENE
        scene = context.scene
        obj = context.active_object
        if _RUNNER.running:
            self.report({"WARNING"}, "已有物理后台任务正在运行")
            return {"CANCELLED"}
        try:
            if obj.override_library is not None:
                raise ValueError("请使用本地可编辑物体")
            state = _workspace(scene)
            output = _native_path(state.output_directory)
            if output:
                base = Path(output)
            elif bpy.data.filepath:
                base = Path(bpy.data.filepath).parent
            else:
                raise ValueError("请选择工作副本位置，或先保存当前工程")
            if not base.is_dir():
                raise ValueError("工作副本位置应为已存在的目录")
            from .native_fluid import assert_isolated_output
            root = Path(mkdtemp(prefix="PNW_", dir=assert_isolated_output(base)))
            request = _background_request(context, obj, self.action, root)
            save = self.action in {"bake", "fracture", "cloth", "native_create", "native_bake"}
            if save:
                candidate = root / "物理工作副本.blend"
            else:
                # Use the output drive so Blender can remap relative library and cache paths.
                _EXPORT_SNAPSHOT = TemporaryDirectory(prefix="pnw_export_", dir=base, ignore_cleanup_errors=True)
                candidate = Path(_EXPORT_SNAPSHOT.name) / "snapshot.blend"
            with ensure_object_mode(context):
                if bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=True, check_existing=False) != {"FINISHED"}:
                    raise RuntimeError("无法保存当前场景的独立快照")
            req, status, result, log = [root / name for name in ("request.json", "status.json", "result.json", "worker.log")]
            title = next(label for action, label, _ in _JOBS if action == self.action)
            workbench_worker.write_json(req, {
                "request": request, "scene": scene.name, "view_layer": context.view_layer.name,
                "detail": f"正在{title}", "save_candidate": save,
            })
            scene_id = scene.session_uid
            _JOB_SCENE = scene

            def complete(ok, payload):
                try:
                    target = next((item for item in bpy.data.scenes if item.session_uid == scene_id), None)
                    if target is None:
                        return
                    if not ok:
                        message = (payload.get("error") or {}).get("message") or "后台进程未完成"
                        _status(target, f"{title}失败：{message}。日志：{log}", "ERROR")
                        return
                    if output_path := payload.get("candidate"):
                        _workspace(target).last_candidate = output_path
                        _status(target, f"{title}已完成，可另开工作副本查看", "OK")
                    else:
                        _status(target, f"{title}已完成", "OK")
                finally:
                    _release_job()

            _RUNNER.start([
                bpy.app.binary_path, "--background", "--factory-startup", "--offline-mode", "--disable-autoexec",
                str(candidate), "--python-exit-code", "1", "--python", str(Path(workbench_worker.__file__)),
                "--", str(req), str(status), str(result), str(log),
            ], label=title, status_file=status, result_file=result, on_complete=complete)
            _status(scene, f"正在后台{title}", "RUNNING")
            return {"FINISHED"}
        except Exception as exc:
            _release_job()
            _status(scene, str(exc), "ERROR")
            self.report({"ERROR"}, "无法启动物理后台任务，请检查下方回执")
            return {"CANCELLED"}


class PNW_OT_cancel_background(bpy.types.Operator):
    bl_idname = "pnw.cancel_background"
    bl_label = "取消物理后台任务"

    def execute(self, context):
        scene = _JOB_SCENE or context.scene
        if _RUNNER.cancel():
            _RUNNER.cleanup()
            _release_job()
            _status(scene, "物理后台任务已取消", "WARN")
        return {"FINISHED"}


class PNW_OT_open_candidate(bpy.types.Operator):
    bl_idname = "pnw.open_candidate"
    bl_label = "另开物理工作副本"
    bl_description = "在新 Blender 进程中打开已完成的副本，保留当前窗口方便对照"

    def execute(self, context):
        path = _workspace(context.scene).last_candidate
        if not path or not Path(path).is_file():
            _status(context.scene, "找不到对应工作副本", "ERROR")
            return {"CANCELLED"}
        try:
            subprocess.Popen([bpy.app.binary_path, "--offline-mode", "--disable-autoexec", path])
        except OSError:
            _status(context.scene, "无法另开工作副本", "ERROR")
            return {"CANCELLED"}
        return {"FINISHED"}


def _native_request(obj, mod, operation, scene_frame):
    settings = obj.pnw_settings
    domain = mod.domain_settings
    preset = _NATIVE_DOMAIN_PRESETS.get(domain.domain_type)
    if preset is None:
        raise ValueError("不支持当前流体域类型")
    request = {
        "schema_version": "1.0",
        "operation": operation,
        "bridge_mode": "MANTAFLOW_BRIDGE",
        "preset_id": preset,
        "target_object": obj.name,
        "modifier_name": mod.name,
        "parameters": {
            "frame_start": domain.cache_frame_start,
            "frame_end": domain.cache_frame_end,
        },
    }
    if operation in {"adopt", "bake"} and settings.native_cache_root:
        request["bake_directory"] = _native_path(settings.native_cache_root)
    if operation == "export":
        if domain.domain_type != "LIQUID":
            raise ValueError("请先选择液体域，再保存定格网格")
        output = _native_path(settings.native_export_path)
        if not output:
            raise ValueError("请选择新的 .blend 输出路径")
        request.update(
            export_format="STATIC_MESH",
            export_path=output,
            export_frame=settings.native_export_frame or scene_frame,
        )
    if operation in {"guide_density", "export_density"}:
        if domain.domain_type != "GAS":
            raise ValueError("请先选择烟气域，再处理密度")
        guide = {}
        if settings.native_guide_name.strip():
            guide["guide_name"] = settings.native_guide_name.strip()
        if operation == "guide_density" and (
            not guide.get("guide_name") or bpy.data.objects.get(guide["guide_name"]) is None
        ):
            if settings.native_bypass is not None:
                guide["bypass_object"] = settings.native_bypass.name
            if settings.native_exit is not None:
                guide["exit_object"] = settings.native_exit.name
        elif operation == "export_density":
            output = _native_path(settings.native_density_output)
            if not output:
                raise ValueError("请选择新的 VDB 输出目录")
            guide["output_directory"] = output
        request["native_density"] = guide
    return request


def _native_summary(settings, result, action):
    lines = [action]
    inspection = result.get("inspection") or {}
    if inspection:
        status = inspection.get("cache_state", "unavailable")
        lines.append(f"Cache: {status}")
        issue = inspection.get("cache_error") or inspection.get("dependency_error")
        if issue:
            lines.append(issue.get("message", issue.get("code", "Check fluid inputs")))
        flows = inspection.get("flows")
        effectors = inspection.get("effectors")
        if isinstance(flows, list) and isinstance(effectors, list):
            lines.append(f"Flow objects: {len(flows)} | Colliders: {len(effectors)}")
    bake = result.get("bake") or {}
    if bake.get("generation"):
        lines.append(f"Generation: {bake['generation']}")
    if isinstance(bake.get("duration_seconds"), (float, int)):
        lines.append(f"Bake time: {bake['duration_seconds']:.2f} s")
    if (result.get("workflow") or {}).get("adopted") or bake.get("saved") is False:
        lines.append("Save the work copy to keep its cache binding")
    if (result.get("invalidate") or {}).get("detached"):
        lines.append("Previous cache files retained")
    exported = result.get("export") or {}
    if exported.get("exported"):
        lines.append(f"Static mesh frame: {exported.get('export_frame')}")
    density = result.get("density") or {}
    if density.get("guide_object"):
        settings.native_guide_name = density["guide_object"]
        lines.append(f"Guide: {density['guide_object']}")
    if density.get("frame_count") is not None:
        lines.append(f"Density frames: {density['frame_count']}")
    return "\n".join(lines)


class PNW_OT_native_fluid_action(bpy.types.Operator):
    bl_idname = "pnw.native_fluid_action"
    bl_label = "管理原生流体域"
    bl_options = {"REGISTER", "UNDO"}

    operation: EnumProperty(name="操作", items=_NATIVE_ACTIONS, default="inspect")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None and obj.library is None and obj.override_library is None
            and _domain_modifier(obj) is not None
        )

    def execute(self, context):
        from . import execute_request

        obj = context.active_object
        settings = obj.pnw_settings
        modifier = _domain_modifier(obj)
        try:
            request = _native_request(obj, modifier, self.operation, context.scene.frame_current)
        except ValueError as exc:
            settings.native_last_summary = str(exc)
            _status(context.scene, str(exc), "ERROR")
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        previous_collections = set(bpy.data.collections)
        with ensure_object_mode(context):
            result = execute_request(request)
        _publish_result(context, result, self.operation)
        if not result.get("ok"):
            error = result.get("error", {})
            message = error.get("message", "Native fluid request failed")
            if error.get("code"):
                message = f"{error['code']}: {message}"
            settings.native_last_summary = message
            self.report({"ERROR"}, _workspace(context.scene).last_report)
            return {"CANCELLED"}
        _theme_result(result, previous_collections)
        title = next(title for action, title, _ in _NATIVE_ACTIONS if action == self.operation)
        settings.native_last_summary = _native_summary(settings, result, f"{title}已完成")
        self.report({"INFO"}, settings.native_last_summary.splitlines()[0])
        return {"FINISHED"}


def _properties(layout, settings, *fields):
    column = prop_column(layout, align=True)
    for field in fields:
        column.prop(settings, field)
    return column


def _job_button(layout, action, text, icon_key, *, enabled=True):
    op = primary_button(layout, "pnw.background_job", text, icon_value=_ICONS.action_id(icon_key),
                        enabled=enabled and not _RUNNER.running)
    op.action = action


def _draw_rigid(layout, obj, settings, state, domain):
    if domain == "rigid_scatter":
        if settings.preset_id == _BOX_TIPPING_PRESET:
            _properties(layout, settings, "box_half_extents", "box_initial_tilt", "activation_frame")
            layout.label(text="在固定地面上计算箱体单轴倾倒", icon="INFO")
        else:
            _properties(layout, settings, "activation_frame", "scatter_count", "sleep_velocity",
                        "contact_iterations", "angular_spin", "stiffness", "rest_length", "anchor_pos")
            _properties(layout, settings, "instance_collection", "instance_normalize")
            layout.operator("pnw.prepare_instances", icon="OUTLINER_COLLECTION")
        layout.separator()
    _properties(layout, state, "fracture_points")
    _properties(layout, settings, "collider_object")
    _job_button(layout, "fracture", "生成并烘焙破碎刚体", "bullet_fracture", enabled=obj.type == "MESH")


def _draw_cloth(layout, obj, settings, state, domain):
    if domain == "cloth_rope":
        _properties(layout, settings, "stiffness", "drag", "constraint_iterations", "curve_resolution",
                    "wind_vector", "wind_turbulence", "bending")
        if settings.bending:
            _properties(layout, settings, "bend_softness")
        _properties(layout, settings, "tearing")
        if settings.tearing:
            _properties(layout, settings, "tear_threshold")
        if obj.type == "MESH":
            row = layout.row(align=True)
            row.operator("pnw.pin_selection", text="固定顶点", icon="PINNED")
            row.operator("pnw.pin_selection", text="解除固定", icon="UNPINNED").unpin = True
        layout.separator()
    _properties(layout, settings, "collider_object", "contact_thickness")
    if obj.type == "MESH":
        layout.prop_search(state, "cloth_pin_group", obj, "vertex_groups", text="固定顶点组")
    _job_button(layout, "cloth", "烘焙原生布料", "cloth_sim", enabled=obj.type == "MESH")


def _draw_native_fluid(layout, context, obj, settings, state):
    if bpy.app.version < (5, 3, 0) or not bpy.app.build_options.fluid:
        layout.label(text="原生流体需要支持 Mantaflow 的 Blender 5.3", icon="INFO")
        return
    domains = [mod for mod in _fluid_modifiers(obj) if mod.fluid_type == "DOMAIN"]
    if not domains:
        _properties(layout, state, "native_domain_type", "native_flow", "native_collider",
                    "native_margin", "native_resolution")
        _job_button(layout, "native_create", "创建原生流体工作副本", "fluid_domain",
                    enabled=obj.type == "MESH" or state.native_flow is not None)
        return
    if len(domains) > 1 or settings.native_modifier_name:
        layout.prop_search(settings, "native_modifier_name", obj, "modifiers", text="流体域修改器")
    mod = _domain_modifier(obj)
    if mod is None:
        layout.label(text="请选择有效的原生流体域修改器", icon="INFO")
        return
    domain = mod.domain_settings
    title = "液体域" if domain.domain_type == "LIQUID" else "烟气域"
    layout.label(text=f"{title} · {mod.name}", icon="MOD_FLUIDSIM")
    controls = prop_column(layout)
    controls.enabled = not domain.has_cache_baked_data and not domain.is_cache_baking_any
    controls.prop(domain, "resolution_max", text="域分辨率")
    controls.prop(domain, "cache_frame_start", text="起始帧")
    controls.prop(domain, "cache_frame_end", text="结束帧")
    controls.prop(domain, "cache_type", text="缓存方式")
    _properties(layout, settings, "native_cache_root")
    row = layout.row(align=True)
    row.operator("pnw.native_fluid_action", text="接管流体域").operation = "adopt"
    row.operator("pnw.native_fluid_action", text="检查缓存").operation = "inspect"
    _job_button(layout, "native_bake", "后台烘焙原生流体", "fluid_domain")
    if domain.has_cache_baked_data:
        layout.operator("pnw.native_fluid_action", text="解除受管缓存", icon="UNLINKED").operation = "invalidate_cache"
    if domain.domain_type == "LIQUID":
        _properties(layout, settings, "native_export_frame", "native_export_path")
        _job_button(layout, "native_export", "导出液体定格", "fluid_domain")
    else:
        layout.prop_search(settings, "native_guide_name", context.scene, "objects", text="密度导向")
        if not settings.native_guide_name or context.scene.objects.get(settings.native_guide_name) is None:
            _properties(layout, settings, "native_bypass", "native_exit")
        layout.operator("pnw.native_fluid_action", text="创建或定位密度导向").operation = "guide_density"
        _properties(layout, settings, "native_density_output")
        _job_button(layout, "density_export", "导出 OpenVDB 烟气序列", "fluid_domain")


class PNW_PT_workbench(bpy.types.Panel):
    bl_label = ""
    bl_idname = "VIEW_3D_PT_physics_nodes_workbench"
    bl_space_type = THEME.editor_space
    bl_region_type = "UI"
    bl_category = THEME.tab_label
    bl_options = {"HEADER_LAYOUT_EXPAND"}

    def draw_header(self, context):
        draw_brand_header(self.layout, DOMAIN, _ICONS, fallback_icon="PHYSICS")

    def draw(self, context):
        layout = self.layout
        state = _workspace(context.scene)
        if _RUNNER.running:
            progress_card(layout, DOMAIN, _ICONS, text=_RUNNER.detail or _RUNNER.label, factor=_RUNNER.progress,
                          cancel_operator="pnw.cancel_background")

        obj = context.active_object
        if obj is None or obj.type not in {"MESH", "CURVE", "CURVES"}:
            layout.label(text="选择网格或曲线以构建物理配方", icon="INFO")
            status_banner(layout, context, state.last_report, kind=state.last_report_kind, icon_pack=_ICONS)
            return
        if obj.library or obj.override_library:
            layout.label(text="请先将目标复制到本地工程", icon="INFO")
            return

        settings = obj.pnw_settings
        managed = _managed_modifier(obj)
        domain = pnw_contract.PRESET_DEFINITIONS.get(settings.preset_id, {}).get("domain")
        is_box = settings.preset_id == _BOX_TIPPING_PRESET
        layout.label(text=obj.name, icon="OBJECT_DATA")
        presets = layout.row(align=True)
        presets.prop(settings, "preset_id", text="")
        presets.operator("pnw.load_preset_defaults", text="", icon="FILE_REFRESH")
        primary_button(layout, "pnw.build_workflow", "构建或更新物理配方",
                       icon_value=_ICONS.action_id("gn_physics"), icon="GEOMETRY_NODES")
        if managed is not None:
            actions = layout.row(align=True)
            actions.operator("pnw.update_parameters", text="应用参数", icon="CHECKMARK")
            actions.operator("pnw.rebuild_workflow", text="重建", icon="FILE_REFRESH")
        forces = prop_column(layout, align=True)
        if is_box:
            forces.prop(settings, "gravity", index=0, text="水平重力")
            forces.prop(settings, "gravity", index=2, text="竖向重力")
        else:
            forces.prop(settings, "gravity")
        forces.prop(settings, "friction")
        forces.prop(settings, "restitution")

        if not bpy.data.filepath:
            layout.prop(state, "output_directory")
        _, rigid = section_panel(layout, "PNW_rigid", "刚体、破碎与接触",
                                  icon_value=_ICONS.action_id("bullet_fracture"), default_closed=True)
        if rigid is not None:
            _draw_rigid(rigid, obj, settings, state, domain)

        _, cloth = section_panel(layout, "PNW_cloth", "布料、绳索与弹性软体",
                                  icon_value=_ICONS.action_id("cloth_sim"), default_closed=True)
        if cloth is not None:
            _draw_cloth(cloth, obj, settings, state, domain)

        _, fluid = section_panel(layout, "PNW_fluid", "原生液体与烟气",
                                  icon_value=_ICONS.action_id("fluid_domain"), default_closed=True)
        if fluid is not None:
            if domain == "fluid_emission":
                _properties(fluid, settings, "emit_rate", "emit_speed", "emit_end_frame", "lifetime",
                            "buoyancy", "sdf_voxel_size")
                fluid.separator()
            _draw_native_fluid(fluid, context, obj, settings, state)

        _, work = section_panel(layout, "PNW_work", "帧范围、求解设置与交付",
                                 icon_value=_ICONS.action_id("gn_physics"), default_closed=True)
        if work is not None:
            frames = work.row(align=True)
            frames.prop(settings, "frame_start")
            frames.prop(settings, "frame_end")
            _properties(work, settings, "substeps", "seed", "ground_z", "particle_radius", "protect_source",
                        "live_parameters", "modifier_name")
            if not is_box:
                _properties(work, settings, "drag", "contact_thickness", "collider_object", "collider_collection")
            if bpy.data.filepath:
                _properties(work, state, "output_directory")
            work.operator("pnw.create_working_copy", icon="DUPLICATE")
            if managed is not None:
                work.separator()
                _properties(work, settings, "bake_directory", "export_format")
                if settings.export_format != "ALEMBIC":
                    _properties(work, settings, "export_frame")
                _properties(work, settings, "export_path")
                _job_button(work, "bake", "后台烘焙缓存", "gn_physics")
                _job_button(work, "export", "后台导出结果", "gn_physics", enabled=bool(settings.export_path))
                row = work.row(align=True)
                row.operator("pnw.inspect_workflow", text="检查模拟", icon="VIEWZOOM")
                row.operator("pnw.invalidate_cache", text="解除缓存", icon="UNLINKED")

        if state.last_candidate:
            layout.operator("pnw.open_candidate", icon="FILE_BLEND")
        status_banner(layout, context, state.last_report, kind=state.last_report_kind, icon_pack=_ICONS)


CLASSES = (
    PNW_WorkbenchSettings,
    PNW_ObjectSettings,
    PNW_OT_load_preset_defaults,
    PNW_OT_build_workflow,
    PNW_OT_create_working_copy,
    PNW_OT_prepare_instances,
    PNW_OT_rebuild_workflow,
    PNW_OT_export_result,
    PNW_OT_pin_selection,
    PNW_OT_update_parameters,
    PNW_OT_bake_cache,
    PNW_OT_invalidate_cache,
    PNW_OT_inspect_workflow,
    PNW_OT_native_fluid_action,
    PNW_OT_background_job,
    PNW_OT_cancel_background,
    PNW_OT_open_candidate,
    PNW_PT_workbench,
)


def register_ui():
    _ICONS.register()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.pnw_settings = PointerProperty(type=PNW_ObjectSettings)
    setattr(bpy.types.Scene, _UI_PROPERTY, PointerProperty(type=PNW_WorkbenchSettings))
    SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)
    bpy.app.handlers.load_pre.append(_cleanup_on_load)


def unregister_ui():
    if _cleanup_on_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_cleanup_on_load)
    _cleanup_on_load(None)
    SidebarDock.unregister_plugin(DOMAIN)
    HUDManager.clear(DOMAIN)
    _ICONS.unregister()
    if hasattr(bpy.types.Scene, _UI_PROPERTY):
        delattr(bpy.types.Scene, _UI_PROPERTY)
    if hasattr(bpy.types.Object, "pnw_settings"):
        del bpy.types.Object.pnw_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
