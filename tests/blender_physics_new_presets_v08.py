"""Inputs and independent output observations for the two new public presets."""
import json
import math
import sys
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
mode, folder = sys.argv[sys.argv.index('--') + 1:]
out = Path(folder).resolve()

if mode == 'prepare':
    out.mkdir(parents=True, exist_ok=False)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mesh = bpy.data.meshes.new('BoxInput')
    mesh.from_pydata([(0, 0, 2)], [], [])
    obj = bpy.data.objects.new('BoxInput', mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=1.2, location=(0, 0, 3))
    bpy.context.object.name = 'ShellInput'
    bpy.ops.wm.save_as_mainfile(filepath=str(out / 'input.blend'))
elif mode == 'observe':
    from physics_nodes_workflow_addon import execute_request, geometry_evidence
    result = json.loads((out / 'batch.json').read_text(encoding='utf-8'))
    plan = json.loads(Path(result['data']['resume_plan']).read_text(encoding='utf-8'))
    records = []
    for candidate in plan['candidates']:
        request = candidate['request']
        bpy.ops.wm.open_mainfile(filepath=request['output_blend'], use_scripts=False)
        obj = bpy.data.objects[request['target_object']]
        check = execute_request({'operation': 'inspect', 'target_object': obj.name,
                                 'preset_id': request['preset_id'], 'parameters': request['parameters']})
        assert check['ok'] and check['inspection']['cache_status']['hash_valid'], check
        assert check['inspection']['sampling']['mode'] == 'CACHED_DIRECT'
        samples = {}
        for frame in (120, 1, 60, 120):
            sample = geometry_evidence.sample_frame(obj, frame)
            if frame in samples:
                assert sample['state_sha256'] == samples[frame]['state_sha256']
            samples[frame] = sample
        assert samples[1]['state_sha256'] != samples[120]['state_sha256']
        if request['preset_id'] == 'rigid_box_tipping':
            geo = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry()
            points = geo.instances_pointcloud()
            assert points is not None and len(points.points) == 1
            # Real box transform and persistent angular state reach the public output.
            assert 'pnw_omega' in points.attributes
            values = [float(v) for row in points.attributes['instance_transform'].data[0].value for v in row]
            assert all(math.isfinite(v) for v in values)
            other = bpy.data.objects['ShellInput']
        else:
            assert len(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices) == 42
            other = bpy.data.objects['BoxInput']
        assert not other.modifiers
        records.append({'preset': request['preset_id'], 'cache_valid': True,
                        'non_target_protected': True, 'samples': samples})
    (out / 'observed.json').write_text(json.dumps({'ok': True, 'records': records}, indent=2), encoding='utf-8')
else:
    raise ValueError(mode)
