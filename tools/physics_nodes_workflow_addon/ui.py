"""Blender 3D Viewport N-Panel Workbench & Operators for Physics Nodes Workflow."""

from __future__ import annotations

import json
import math
from typing import Any

import bpy
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
from .capabilities import QUALITY_NOTES


_BOX_TIPPING_PRESET = "rigid_box_tipping"


def _preset_items(self, context):
    return [
        (pid, info["title"], info["description"])
        for pid, info in pnw_contract.PRESET_DEFINITIONS.items()
    ]


class PNW_ObjectSettings(bpy.types.PropertyGroup):
    preset_id: EnumProperty(
        name="Preset",
        description="Physics domain preset",
        items=_preset_items,
    )
    bridge_mode: EnumProperty(
        name="Solver Mode",
        description="Geometry Nodes native kernel or explicit bridge to Blender physics",
        items=[
            ("GN_NATIVE", "GN Native (Modular)", "Modular Geometry Nodes Simulation + Repeat substeps"),
            ("XPBD_ASSET_BRIDGE", "XPBD Bridge (Reserved)", "Reserved adapter; GN cloth uses bundled XPBD internally"),
            ("BULLET_BRIDGE", "Bullet Bridge (Reserved)", "Reserved adapter, not implemented"),
            ("MANTAFLOW_BRIDGE", "Mantaflow Bridge (Reserved)", "Reserved adapter, not implemented"),
        ],
        default="GN_NATIVE",
    )
    modifier_name: StringProperty(name="Modifier Name", default="PhysicsNodes")
    collider_object: PointerProperty(name="Collider", type=bpy.types.Object)
    collider_collection: PointerProperty(name="Collider Collection", type=bpy.types.Collection)
    instance_collection: PointerProperty(name="Instances", type=bpy.types.Collection)
    instance_normalize: BoolProperty(name="Fit Instances to Radius", default=True)
    protect_source: BoolProperty(name="Build on Working Copy", default=True,
        description="The first build creates an independent copy; later builds update that copy")

    substeps: IntProperty(name="Substeps", default=4, min=1, max=8)
    contact_iterations: IntProperty(name="Contact Iterations", default=3, min=1, max=32)
    constraint_iterations: IntProperty(name="Constraint Iterations", default=12, min=1, max=64)
    seed: IntProperty(name="Seed", default=42, min=0)
    frame_start: IntProperty(name="Frame Start", default=1, min=1)
    frame_end: IntProperty(name="Frame End", default=120, min=1)
    activation_frame: IntProperty(
        name="Activation Frame", default=0, min=0, max=100000,
        description="Absolute scene frame where the whole rigid group activates; 0 starts immediately, earlier frames clamp to Frame Start",
    )

    gravity: FloatVectorProperty(name="Gravity", default=(0.0, 0.0, -9.81), size=3)
    drag: FloatProperty(name="Drag", default=0.04, min=0.0, max=20.0)
    restitution: FloatProperty(name="Restitution", default=0.35, min=0.0, max=1.0)
    friction: FloatProperty(name="Friction", default=0.45, min=0.0, max=1.0)
    sleep_velocity: FloatProperty(name="Sleep Velocity", default=0.05, min=0.0, max=10.0)
    contact_thickness: FloatProperty(name="Contact Thickness", default=0.08, min=0.001, max=2.0)
    particle_radius: FloatProperty(name="Particle Radius", default=0.12, min=0.001, max=5.0)
    ground_z: FloatProperty(name="Ground Z", default=0.0)
    box_half_extents: FloatVectorProperty(
        name="Box Half Extents",
        description="Planar box half sizes (hx, hy, hz); each component must be positive",
        default=(0.2, 0.3, 1.0),
        size=3,
        min=1e-6,
    )
    box_initial_tilt: FloatProperty(
        name="Box Initial Tilt",
        description="Deterministic initial rotation around Y in radians",
        subtype="ANGLE",
        default=0.3,
        min=-math.pi / 2.0 + 1e-6,
        max=math.pi / 2.0 - 1e-6,
    )

    scatter_count: IntProperty(name="Scatter Count", default=48, min=0, max=10000)
    angular_spin: FloatProperty(name="Angular Spin", default=1.2, min=0.0, max=50.0)
    stiffness: FloatProperty(name="Stiffness", default=0.7, min=0.0, max=1.0)
    bending: BoolProperty(name="Cloth Bending", default=False,
        description="Cross-edge constraints for mesh faces; not a rod twist solver")
    bend_softness: FloatProperty(name="Bend Softness", default=0.1, min=0.0, max=1.0)
    tearing: BoolProperty(name="Cloth Tearing", default=False,
        description="Split mesh face edges when relative strain exceeds the threshold")
    tear_threshold: FloatProperty(name="Tear Strain Threshold", default=1.2, min=1.0, max=100.0)
    anchor_pos: FloatVectorProperty(name="Anchor", default=(0.0, 0.0, 2.5), size=3)
    rest_length: FloatProperty(name="Rest Length", default=0.0, min=0.0)
    curve_resolution: IntProperty(name="Curve Resolution", default=64, min=2, max=50000)

    wind_vector: FloatVectorProperty(name="Wind", default=(0.0, 0.0, 0.0), size=3)
    wind_turbulence: FloatProperty(name="Turbulence", default=0.0, min=0.0, max=50.0)

    emit_rate: IntProperty(name="Emit Rate", default=24, min=0, max=5000)
    emit_speed: FloatProperty(name="Emit Speed", default=6.0, min=0.0, max=100.0)
    emit_end_frame: IntProperty(name="Emit End Frame", default=90, min=1)
    lifetime: IntProperty(name="Lifetime (Frames)", default=110, min=1)
    buoyancy: FloatProperty(name="Buoyancy", default=0.0, min=-50.0, max=50.0)
    sdf_voxel_size: FloatProperty(name="SDF Voxel Size", default=0.08, min=0.01, max=2.0)

    bake_directory: StringProperty(name="Bake Directory", subtype="DIR_PATH", default="")
    export_format: EnumProperty(name="Export", items=[
        ("STATIC_MESH", "Static Mesh (.blend)", "Realize the selected frame into a mesh copy"),
        ("ALEMBIC", "Alembic (.abc)", "Export the frame range"),
        ("OPENVDB", "OpenVDB (.vdb)", "Export the evaluated surface as a volume grid")], default="STATIC_MESH")
    export_path: StringProperty(name="Output File", subtype="FILE_PATH", default="")
    export_frame: IntProperty(name="Export Frame", default=1, min=1)
    last_inspect_summary: StringProperty(name="Last Inspection", default="")
    last_export_summary: StringProperty(name="Last Export", default="")
    last_instance_summary: StringProperty(name="Prepared Instances", default="")

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
        operator.report({"ERROR"}, "Select an active target object first")
        return {"CANCELLED"}
    req = obj.pnw_settings.to_request(obj, operation, bake=bake)
    if operation == "inspect":
        req = _inspection_request(obj, req)
    req["force_rebuild"] = force_rebuild
    res = execute_request(req)
    if not res.get("ok"):
        obj.pnw_settings.last_inspect_summary = "Diagnostics unavailable: " + res.get("error", {}).get("message", "Execution failed")
        operator.report({"ERROR"}, res.get("error", {}).get("message", "Execution failed"))
        return {"CANCELLED"}
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
            operator.report({"WARNING"}, insp.get("sampling", {}).get("warning") or lines[0])
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
    operator.report({"INFO"}, f"Physics Nodes [{operation}] finished")
    return {"FINISHED"}


class PNW_OT_load_preset_defaults(bpy.types.Operator):
    bl_idname = "pnw.load_preset_defaults"
    bl_label = "Load Preset Defaults"
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
        return {"FINISHED"}


class PNW_OT_build_workflow(bpy.types.Operator):
    bl_idname = "pnw.build_workflow"
    bl_label = "Build / Update Physics Nodes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type in {"MESH", "CURVE", "CURVES"} and context.active_object.mode == "OBJECT"

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
    bl_label = "Create Physics Working Copy"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type in {"MESH", "CURVE", "CURVES"}

    def execute(self, context):
        source = context.active_object
        if source.mode != "OBJECT":
            self.report({"ERROR"}, "Switch to Object Mode before creating a working copy")
            return {"CANCELLED"}
        working = source.copy()
        working.data = source.data.copy()
        working.name = source.name + "_Physics"
        working["pnw_working_copy_source"] = source.name_full
        context.collection.objects.link(working)
        for item in context.selected_objects:
            item.select_set(False)
        working.select_set(True)
        context.view_layer.objects.active = working
        result = _run_op(self, context, "build")
        if result != {"FINISHED"}:
            # Keep the recoverable copy for diagnosis; never change the source.
            self.report({"WARNING"}, "Working copy retained; see the build error")
        return result


class PNW_OT_prepare_instances(bpy.types.Operator):
    bl_idname = "pnw.prepare_instances"
    bl_label = "Prepare Prototype Copies"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.library is None and obj.mode == "OBJECT" and obj.pnw_settings.instance_collection is not None

    def execute(self, context):
        from . import execute_request
        obj = context.active_object
        result = execute_request(obj.pnw_settings.to_request(obj, "prepare_instances"))
        if not result.get("ok"):
            self.report({"ERROR"}, result.get("error", {}).get("message", "Cannot prepare instances"))
            return {"CANCELLED"}
        prepared = result["prepared_instances"]
        obj.pnw_settings.instance_collection = bpy.data.collections[prepared["prepared_collection"]]
        obj.pnw_settings.last_instance_summary = (f"Prepared {len(prepared['objects'])} independent mesh copies.\n"
            "Source stays unchanged; shaders remain shared.\nBuild / Update to use the selected copies.")
        self.report({"INFO"}, "Prototype copies prepared; Build / Update to use them")
        return {"FINISHED"}


class PNW_OT_rebuild_workflow(bpy.types.Operator):
    bl_idname = "pnw.rebuild_workflow"
    bl_label = "Rebuild (Retain Old Graph)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.mode != "OBJECT":
            return False
        mod = obj.modifiers.get(obj.pnw_settings.modifier_name)
        return mod is not None and mod.type == "NODES" and mod.node_group is not None and mod.node_group.get("pnw_role") == "wrapper"

    def execute(self, context):
        return _run_op(self, context, "build", force_rebuild=True)


class PNW_OT_export_result(bpy.types.Operator):
    bl_idname = "pnw.export_result"
    bl_label = "Export Physics Result"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if context.active_object is None or not context.active_object.pnw_settings.export_path:
            self.report({"ERROR"}, "Choose a new output file path before exporting")
            return {"CANCELLED"}
        return _run_op(self, context, "export")


class PNW_OT_pin_selection(bpy.types.Operator):
    bl_idname = "pnw.pin_selection"
    bl_label = "Pin Selected Vertices"
    bl_options = {"REGISTER", "UNDO"}
    unpin: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        import bmesh
        obj = context.active_object
        if obj.library or obj.data.library:
            self.report({"ERROR"}, "Create a local working copy before authoring pins")
            return {"CANCELLED"}
        value = 0.0 if self.unpin else 1.0
        if obj.mode == "EDIT":
            if obj.data.users > 1:
                self.report({"ERROR"}, "Make mesh data single-user before editing pins")
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
                self.report({"ERROR"}, "pnw_pinned must be a FLOAT attribute on POINT domain")
                return {"CANCELLED"}
            attr = attr or obj.data.attributes.new("pnw_pinned", "FLOAT", "POINT")
            for v in obj.data.vertices:
                if v.select:
                    attr.data[v.index].value = value
            obj.data.update()
        return {"FINISHED"}


class PNW_OT_update_parameters(bpy.types.Operator):
    bl_idname = "pnw.update_parameters"
    bl_label = "Apply Parameters (Keep Edits)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return _run_op(self, context, "parameters")


class PNW_OT_bake_cache(bpy.types.Operator):
    bl_idname = "pnw.bake_cache"
    bl_label = "Bake to Disk"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "bake", bake=True)


class PNW_OT_invalidate_cache(bpy.types.Operator):
    bl_idname = "pnw.invalidate_cache"
    bl_label = "Invalidate Cache"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "invalidate_cache")


class PNW_OT_inspect_workflow(bpy.types.Operator):
    bl_idname = "pnw.inspect_workflow"
    bl_label = "Inspect Simulation"
    bl_options = {"REGISTER"}

    def execute(self, context):
        return _run_op(self, context, "inspect")


class PNW_PT_workbench(bpy.types.Panel):
    bl_label = "Physics Nodes Workbench"
    bl_idname = "VIEW_3D_PT_physics_nodes_workbench"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Physics Nodes"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if obj is None:
            layout.label(text="Select a target object", icon="INFO")
            return

        s = obj.pnw_settings
        domain = pnw_contract.PRESET_DEFINITIONS[s.preset_id]["domain"]
        is_box_tipping = s.preset_id == _BOX_TIPPING_PRESET
        if is_box_tipping:
            layout.label(text="Planar box-floor: fixed ground, single-point contact", icon="INFO")
        else:
            layout.label(text=QUALITY_NOTES[domain], icon="INFO")
        box = layout.box()
        box.label(text="Domain & Solver", icon="PHYSICS")
        row = box.row(align=True)
        row.prop(s, "preset_id", text="")
        row.operator("pnw.load_preset_defaults", text="", icon="FILE_REFRESH")
        if is_box_tipping:
            box.label(text="Solver: GN Native box-floor impulse (fixed)", icon="NODETREE")
        else:
            box.prop(s, "bridge_mode")
        box.prop(s, "modifier_name")
        if not is_box_tipping:
            box.prop(s, "collider_object")
            box.prop(s, "collider_collection")
        if domain == "rigid_scatter" and not is_box_tipping:
            box.prop(s, "instance_collection")
            box.operator("pnw.prepare_instances")
            if s.last_instance_summary:
                for line in s.last_instance_summary.splitlines():
                    box.label(text=line)
            box.prop(s, "instance_normalize")

        box_sim = layout.box()
        box_sim.label(text="Time & Determinism", icon="TIME")
        row = box_sim.row(align=True)
        row.prop(s, "frame_start")
        row.prop(s, "frame_end")
        row = box_sim.row(align=True)
        row.prop(s, "substeps")
        if not is_box_tipping:
            row.prop(s, "seed")

        box_phys = layout.box()
        box_phys.label(text="Forces & Contact", icon="FORCE_WIND")
        if is_box_tipping:
            box_phys.label(text="Planar gravity / dry floor contact", icon="INFO")
            box_phys.prop(s, "gravity", index=0, text="Gravity X")
            box_phys.prop(s, "gravity", index=2, text="Gravity Z")
            box_phys.prop(s, "ground_z")
            box_phys.prop(s, "restitution")
            box_phys.prop(s, "friction")
        else:
            box_phys.prop(s, "gravity")
            box_phys.prop(s, "drag")
            box_phys.prop(s, "restitution")
            box_phys.prop(s, "friction")
            box_phys.prop(s, "sleep_velocity")
            box_phys.prop(s, "contact_thickness")
            box_phys.prop(s, "particle_radius")
            box_phys.prop(s, "ground_z")
            box_phys.prop(s, "wind_vector")
            box_phys.prop(s, "wind_turbulence")

        if domain == "rigid_scatter" and is_box_tipping:
            box_d = layout.box()
            box_d.label(text="Rigid Box Floor Tipping (Planar)", icon="RIGID_BODY")
            box_d.prop(s, "box_half_extents")
            box_d.prop(s, "box_initial_tilt")
            box_d.prop(s, "activation_frame")
            box_d.label(text="Fixed horizontal ground plane", icon="INFO")
            box_d.label(text="No box-box / arbitrary mesh / non-planar motion")
            box_d.label(text="Single-point support: side-lying rocking may occur")
        elif domain == "rigid_scatter":
            box_d = layout.box()
            box_d.label(text="Rigid Scatter & Tether", icon="RIGID_BODY")
            box_d.prop(s, "activation_frame")
            box_d.prop(s, "scatter_count")
            box_d.prop(s, "contact_iterations")
            box_d.prop(s, "angular_spin")
            box_d.prop(s, "stiffness")
            box_d.prop(s, "rest_length")
            box_d.prop(s, "anchor_pos")
        elif domain == "cloth_rope":
            box_d = layout.box()
            box_d.label(text="Cloth & Rope Elasticity", icon="MOD_CLOTH")
            box_d.prop(s, "stiffness")
            box_d.prop(s, "curve_resolution")
            box_d.prop(s, "constraint_iterations")
            box_d.prop(s, "bending")
            if s.bending:
                box_d.prop(s, "bend_softness")
            box_d.prop(s, "tearing")
            if s.tearing:
                box_d.prop(s, "tear_threshold")
            row = box_d.row(align=True)
            row.operator("pnw.pin_selection", text="Pin Selected")
            row.operator("pnw.pin_selection", text="Unpin Selected").unpin = True
            box_d.label(text="No automatic pins; author pnw_pinned")
        else:
            box_d = layout.box()
            box_d.label(text="Fluid & Gas Emission", icon="MOD_FLUIDSIM")
            box_d.prop(s, "emit_rate")
            box_d.prop(s, "emit_speed")
            box_d.prop(s, "emit_end_frame")
            box_d.prop(s, "lifetime")
            box_d.prop(s, "buoyancy")
            box_d.prop(s, "sdf_voxel_size")

        box_act = layout.box()
        box_act.label(text="Workflow Actions", icon="PLAY")
        box_act.prop(s, "bake_directory")
        box_act.prop(s, "protect_source")
        box_act.operator("pnw.create_working_copy", icon="DUPLICATE")
        box_act.operator("pnw.build_workflow", icon="NODETREE")
        box_act.operator("pnw.update_parameters", icon="CHECKMARK")
        box_act.operator("pnw.rebuild_workflow", icon="FILE_REFRESH")
        row = box_act.row(align=True)
        row.operator("pnw.bake_cache", icon="DISK_DRIVE")
        row.operator("pnw.invalidate_cache", icon="TRASH")
        box_act.operator("pnw.inspect_workflow", icon="VIEWZOOM")
        if s.last_inspect_summary:
            for line in s.last_inspect_summary.splitlines():
                box_act.label(text=line)

        box_export = layout.box()
        box_export.label(text="Export Result", icon="EXPORT")
        box_export.prop(s, "export_format", text="")
        if s.export_format != "ALEMBIC":
            box_export.prop(s, "export_frame")
        box_export.prop(s, "export_path")
        box_export.operator("pnw.export_result")
        if s.last_export_summary:
            for line in s.last_export_summary.splitlines():
                box_export.label(text=line)


CLASSES = (
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
    PNW_PT_workbench,
)


def register_ui():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.pnw_settings = PointerProperty(type=PNW_ObjectSettings)


def unregister_ui():
    if hasattr(bpy.types.Object, "pnw_settings"):
        del bpy.types.Object.pnw_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
