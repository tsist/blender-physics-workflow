"""Workflow availability and solver quality are separate product dimensions."""
from __future__ import annotations

IMPLEMENTATION_VERSION = "0.9.0-dev.2"
# Native fluid support keeps the existing Geometry Nodes graph layout.
GRAPH_IMPLEMENTATION_VERSION = "0.8.1-dev.1"
QUALITY_NOTES = {
    "rigid_scatter": "Sphere proxies; 200-body container rest qualified, arbitrary dense stacks remain approximate",
    "cloth_rope": "XPBD edges, mesh bending/tearing; no self-collision",
    "fluid_emission": "Particle preview; no liquid pressure solver",
}


def describe_capabilities():
    return {
        "implementation_version": IMPLEMENTATION_VERSION,
        "release_status": "development_candidate",
        "coordinates": "world_space_full_matrix",
        "physics_evidence_schema": "physics-geometry-evidence-v2",
        "physics_attribute_scope": "available_top_level_evaluated_output_components",
        "inspection_ground": "world_plane_evaluated_mesh_vertices_and_instance_origins_separate",
        "inspection_motion": "missing_or_incomparable_positions_return_null_not_resting",
        "inspection_sleep": "explicit_top_level_pnw_sleep_separate_from_low_speed_fraction",
        "internal_solver_state_compared": False,
        "pinning": "explicit_POINT_FLOAT_pnw_pinned",
        "legacy_migration": "explicit_force_rebuild_retains_previous_graph",
        "native_bridges": "mantaflow_explicit_native_presets",
        "openvdb_export": "verified_surface_level_set_of_closed_world_mesh",
        "alembic_export": "realized_single_mesh_sequence",
        "export_delivery_schema": "pnw.export.delivery/1",
        "static_materials": "effective_target_slots_shader_graphs_and_image_inventory",
        "alembic_materials": "face_sets_only_no_shaders_or_images",
        "alembic_uvs": "multiple_uv_maps_blender_reader_verified_selection_not_preserved",
        "prototype_object_material_overrides": "use_explicit_prepare_instances_raw_overrides_still_rejected",
        "prototype_preparation": "explicit_local_static_mesh_snapshots_preserve_effective_slots",
        "prototype_snapshot_source": "provenance_only_no_live_sync",
        "prototype_snapshot_shaders": "shared_material_datablocks",
        "native_volume_density_exported": True,
        "native_fluid_candidate": {
            "host_minimum": "5.3.0",
            "domain_types": ["LIQUID", "GAS"],
            "input": "existing_authored_fluid_domain",
            "evidence": "scoped_by_operation",
            "cache": "mantaflow_all_signed_generations_with_separate_archive",
            "density": "fixed_openvdb_gas_float_fog_sequence",
        },
        "fluid_particle_count": "actual_simulation_population_on_reconstructed_mesh_pnw_particle_count",
        "cloth_bending": "optional_native_cross_edge_constraint_for_mesh_faces",
        "cloth_tearing": "optional_native_strain_based_split_edges",
        "softbody_elastic_shell": "closed_triangulated_surface_shell_with_bending_not_solid_volume",
        "rigid_box_tipping": "unit_mass_XZ_translation_Y_rotation_fixed_ground_max_64_no_box_box",
        "box_tipping_rest": "single_point_contact_can_rock_or_creep_not_qualified_as_static_rest",
        "visible_ui": "build_bake_inspect_parameter_edit_save_cold_verified_for_box_tipping",
        "representative_loads": "200_body_container_and_2000_sparse_250_frames_10000_cloth_vertices_120_frames_52520_particles_250_frames",
        "sphere_contact_sleep": "downward_gravity_supported_rest_with_conservative_wake_not_general_contact_islands",
        "rigid_activation": "whole_workflow_at_scene_frame_zero_immediate",
        "persistent_resume": True,
        "resume_manifest_schema": "pnw.workflow.resume_plan",
        "resume_manifest_version": 1,
        "resume_execution_mode": "candidate_replay",
        "intermediate_frame_resume": False,
        "resume_lock": "original_writers_and_resume_share_os_file_lock",
        "workflow_acceptance": "module_scoped_function_and_output_verification",
        "solver_quality": {
            domain: {"status": "approximate", "summary": text}
            for domain, text in QUALITY_NOTES.items()
        },
        "limitations": [
            {"id": "dense_proxy_contacts", "owner": "plugin_solver", "status": "known_quality_limit",
             "blocks_unrelated_workflows": False,
             "detail": "200-body container/ramp/trigger scene settles within 1e-4; 27-ball residual regression passes but does not establish universal static rest or arbitrary contact-island tracking"},
            {"id": "xpbd_backend", "owner": "blender_backend", "status": "experimental_backend",
             "blocks_unrelated_workflows": False,
             "detail": "Blender 5.2 bundled XPBD/cloth is experimental; supported input adapters are tested independently"},
            {"id": "liquid_pressure", "owner": "plugin_solver", "status": "not_implemented",
             "blocks_unrelated_workflows": False,
             "detail": "Particle emission and surface reconstruction do not solve incompressibility"},
            {"id": "native_bridges", "owner": "contract", "status": "native_fluid_available",
             "blocks_unrelated_workflows": False,
             "detail": "Use a native liquid or gas preset to select Mantaflow; legacy bridge flags alone do not switch solvers."},
        ],
    }


def runtime_capabilities():
    """Read build availability without changing the Blender environment."""
    import importlib.util
    import bpy
    return {
        "blender_version": bpy.app.version_string,
        "openvdb_build": bool(bpy.app.build_options.openvdb),
        "openvdb_python": importlib.util.find_spec("openvdb") is not None,
        "alembic_build": bool(bpy.app.build_options.alembic),
        "qualification": describe_capabilities(),
    }
