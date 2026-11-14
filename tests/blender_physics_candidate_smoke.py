"""Run with Blender --background --factory-startup --python-exit-code 1.

Arguments after --: build|cold ADDON_PARENT EVIDENCE_DIR.
This tests an extracted package without installing it or touching user sessions.
"""
from pathlib import Path
import hashlib
import json
import math
import sys
import bpy

mode, addon_parent, evidence = sys.argv[sys.argv.index('--') + 1:]
out = Path(evidence).resolve()
if mode == 'build':
    out.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(Path(addon_parent).resolve()))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import bake, contract, geometry_evidence, subgroups
assert Path(addon.__file__).resolve().is_relative_to(Path(addon_parent).resolve())
addon.register()

def cache_valid(obj, request):
    mod = obj.modifiers['PhysicsNodes']
    core = mod.node_group.nodes['PNW_CoreNode'].node_tree
    valid, reason, _ = bake.is_cache_valid(obj, mod, request['parameters_hash'], subgroups.tree_fingerprint(core))
    assert valid, reason
    return reason

def origins(obj, frame):
    bpy.context.scene.frame_set(frame)
    graph = bpy.context.evaluated_depsgraph_get()
    return [tuple(i.matrix_world.translation) for i in graph.object_instances
            if i.is_instance and i.parent and i.parent.original == obj]

if mode == 'build':
    scene = bpy.context.scene
    scene.render.fps = 24
    mesh = bpy.data.meshes.new('CandidateSpheres')
    mesh.from_pydata([(0, 0, 2), (0, 0, 3)], [], [])
    obj = bpy.data.objects.new('CandidateSpheres', mesh)
    scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    request = {
        'target_object': obj.name, 'preset_id': 'rigid_drop_pack', 'bake': True,
        'bake_directory': str(out / 'cache'),
        'parameters': {'frame_end': 60, 'scatter_count': 0, 'particle_radius': .2,
                       'wind_vector': [0, 0, 0], 'wind_turbulence': 0,
                       'stiffness': 0, 'rest_length': 0, 'restitution': 0},
    }
    bpy.ops.wm.save_as_mainfile(filepath=str(out / 'source.blend'))
    source_hash = hashlib.sha256((out / 'source.blend').read_bytes()).hexdigest()
    result = addon.execute_request(request)
    assert result['ok'] and result['bake']['baked'], result
    req = contract.normalize_request(request)
    cache_valid(obj, req)
    evidence = geometry_evidence.sample_frames(obj, [1, 30, 60])
    pos = origins(obj, 60)
    assert len(pos) == 2 and math.dist(*pos) >= .4 - 1e-4, pos
    assert min(p[2] for p in pos) >= .2 - 1e-4, pos
    bpy.ops.wm.save_as_mainfile(filepath=str(out / 'output.blend'))
    cache_valid(obj, req)
    assert hashlib.sha256((out / 'source.blend').read_bytes()).hexdigest() == source_hash
    payload = {'pass': True, 'module': addon.__file__, 'parameters_hash': req['parameters_hash'],
               'request': request, 'geometry': evidence, 'f60_origins': pos,
               'source_unchanged': True, 'template_revision': subgroups.SUBGROUP_REVISION}
    (out / 'build.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print('CANDIDATE_BUILD_PASS')
elif mode == 'cold':
    expected = json.loads((out / 'build.json').read_text(encoding='utf-8'))
    bpy.ops.wm.open_mainfile(filepath=str(out / 'output.blend'), use_scripts=False)
    obj = bpy.data.objects[expected['request']['target_object']]
    req = contract.normalize_request(expected['request'])
    reason = cache_valid(obj, req)
    matches = []
    for frame in [60, 1, 30, 60]:
        actual = geometry_evidence.sample_frame(obj, frame)
        wanted = expected['geometry']['frames'][str(frame)]
        matches.append(all(actual[field] == wanted[field] for field in
                           geometry_evidence.COUNT_FIELDS + geometry_evidence.DIGEST_FIELDS
                           + ('evidence_schema', 'state_coverage')))
    assert all(matches), matches
    payload = {'pass': True, 'cache_reason': reason, 'frames': [60, 1, 30, 60],
               'state_matches': matches, 'module': addon.__file__,
               'scope': 'Mesh/topology/instances and available top-level pnw attributes; internal solver state excluded'}
    (out / 'cold.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    print('CANDIDATE_COLD_PASS')
else:
    raise ValueError(mode)
addon.unregister()
