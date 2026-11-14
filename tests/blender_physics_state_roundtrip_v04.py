"""Managed three-domain bake/save/cold output-state evidence (small fixtures).

Arguments: build|cold ADDON_PARENT CLI_DIR EVIDENCE_DIR.
Uses the exact supplied modules, so this also verifies extracted candidates.
"""
from pathlib import Path
import json
import sys
import bpy

mode, addon_parent, cli_dir, folder = sys.argv[sys.argv.index('--')+1:]
sys.path[:0] = [str(Path(addon_parent).resolve()), str(Path(cli_dir).resolve())]
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import geometry_evidence, contract, bake, subgroups
import physics_verifier

OUT = Path(folder).resolve()
addon.register()
bpy.context.preferences.filepaths.file_preview_type = 'NONE'
bpy.context.preferences.filepaths.save_version = 0

def valid(obj, request):
    mod = obj.modifiers['PhysicsNodes']
    core = mod.node_group.nodes['PNW_CoreNode'].node_tree
    ok, reason, _ = bake.is_cache_valid(obj, mod, contract.normalize_request(request)['parameters_hash'],
                                      subgroups.tree_fingerprint(core))
    assert ok, reason

if mode == 'build':
    OUT.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 24
    records = {}
    fixtures = [
        ('Rigid', 'rigid_drop_pack', [(0,0,2), (1,0,3)], [], {'scatter_count':0, 'rest_length':0, 'stiffness':0}),
        ('Cloth', 'cloth_banner_wind', [(x*.3,y*.3,2) for y in range(3) for x in range(3)],
         [(a,a+1,a+4,a+3) for a in (0,1,3,4)], {'constraint_iterations':3}),
        ('Fluid', 'fluid_fountain_sdf', [(-.1,-.1,2),(.1,-.1,2),(.1,.1,2),(-.1,.1,2)],
         [(0,1,2,3)], {'emit_rate':2, 'emit_end_frame':24, 'lifetime':60, 'sdf_voxel_size':.15}),
    ]
    for name, preset, points, faces, extra in fixtures:
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(points, [], faces)
        if name == 'Cloth':
            pins = mesh.attributes.new('pnw_pinned', 'FLOAT', 'POINT')
            for i, item in enumerate(pins.data): item.value = 1.0 if i % 3 == 0 else 0.0
        obj = bpy.data.objects.new(name, mesh)
        scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        request = {'target_object':name, 'preset_id':preset, 'bake':True,
                   'bake_directory':str(OUT/name), 'parameters':{
                       'frame_start':1, 'frame_end':24, 'substeps':2, 'wind_turbulence':0,
                       'wind_vector':[0,0,0], **extra}}
        bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'work.blend'))
        result = addon.execute_request(request)
        assert result['ok'] and result['bake']['baked'], result
        valid(obj, request)
        samples = geometry_evidence.sample_frames(obj, [1,12,24])
        assert all(s['evidence_schema'] == 'physics-geometry-evidence-v2' for s in samples['frames'].values())
        if name != 'Fluid':
            assert samples['frames']['24']['physics_attributes']['velocity_statistics']['count'] > 0
        else:
            assert samples['frames']['24']['has_geometry']
        records[name] = {'request':request, 'samples':samples}
        # Exercise the actual UI inspection operator, including source labeling.
        obj.pnw_settings.preset_id = preset
        for key, value in contract.normalize_request(request)['parameters'].items():
            if hasattr(obj.pnw_settings,key):
                setattr(obj.pnw_settings,key,value)
        assert bpy.ops.pnw.inspect_workflow() == {'FINISHED'}
        assert 'Output physics attributes:' in obj.pnw_settings.last_inspect_summary
        assert obj.pnw_settings.last_inspect_summary.startswith('Diagnostics valid'), obj.pnw_settings.last_inspect_summary
        if name == 'Rigid':
            original = obj.pnw_settings.gravity[:]
            obj.pnw_settings.gravity[2] -= 1
            assert bpy.ops.pnw.inspect_workflow() == {'FINISHED'}
            assert obj.pnw_settings.last_inspect_summary.startswith('Diagnostics invalid')
            obj.pnw_settings.gravity = original
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'output.blend'))
    (OUT/'build.json').write_text(json.dumps({'pass':True, 'records':records},indent=2),encoding='utf-8')
    print('STATE_ROUNDTRIP_BUILD_PASS')
elif mode == 'cold':
    expected = json.loads((OUT/'build.json').read_text(encoding='utf-8'))
    bpy.ops.wm.open_mainfile(filepath=str(OUT/'output.blend'),use_scripts=False)
    results = {}
    for name, record in expected['records'].items():
        obj = bpy.data.objects[name]
        valid(obj, record['request'])
        rows = []
        for frame in [24,1,12,24]:
            actual = geometry_evidence.sample_frame(obj,frame)
            wanted = record['samples']['frames'][str(frame)]
            checks, failures = physics_verifier._compare_summary(actual,wanted,frame)
            assert not failures, (name, frame, [c for c in checks if not c['passed']])
            rows.append({'frame':frame, 'pass':True, 'coverage':actual['state_coverage'],
                         'attributes':actual['physics_attribute_names']})
        results[name] = rows
    (OUT/'cold.json').write_text(json.dumps({'pass':True,'results':results},indent=2),encoding='utf-8')
    print('STATE_ROUNDTRIP_COLD_PASS')
else:
    raise ValueError(mode)
addon.unregister()
