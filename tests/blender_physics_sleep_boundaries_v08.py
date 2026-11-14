"""Public solver regressions: a free cluster must fall and removed support must wake."""
import json
from pathlib import Path
import sys

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from physics_nodes_workflow_addon import execute_request

out = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
out.mkdir(parents=True, exist_ok=False)
records = {}

def point_object(name, points):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(points, [], [])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj

def state(obj):
    geo = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry()
    points = geo.instances_pointcloud()
    sleeping = points.attributes.get('pnw_sleep')
    assert sleeping is not None or bpy.context.scene.frame_current == 1
    return {'positions': [[float(x) for x in d.value.translation] for d in points.attributes['instance_transform'].data],
            'velocity': [[float(x) for x in d.vector] for d in points.attributes['pnw_velocity'].data],
            'sleep': ([bool(d.value) for d in sleeping.data] if sleeping is not None
                      else [False] * len(points.attributes['instance_transform'].data))}

def request(obj, **changes):
    parameters = {'frame_start': 1, 'frame_end': 16, 'scatter_count': 0, 'substeps': 1,
        'particle_radius': .2, 'contact_thickness': .2, 'ground_z': -100,
        'gravity': [0, 0, -9.81], 'drag': 0, 'wind_vector': [0, 0, 0], 'wind_turbulence': 0,
        'stiffness': 0, 'rest_length': 0, 'friction': .45, 'restitution': 0, 'sleep_velocity': .08}
    parameters.update(changes)
    return {'target_object': obj.name, 'preset_id': 'rigid_drop_pack', 'parameters': parameters}

for label, separation in [('touching', .4), ('gap', .402)]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = 1000
    obj = point_object(label, [(0, 0, 2), (0, 0, 2 + separation)])
    result = execute_request(request(obj))
    assert result['ok'], result
    rows = {}
    for frame in range(1, 9):
        scene.frame_set(frame)
        rows[str(frame)] = state(obj)
    initial = rows['1']['positions']
    last = rows['8']
    expected_delta = -9.81 / 1000 ** 2 * 7 * 8 / 2
    checks = {'no_sleep': not any(any(row['sleep']) for row in rows.values()),
              'free_fall': all(abs(b[2] - a[2] - expected_delta) < 2e-6 for a, b in zip(initial, last['positions']))}
    records[label] = {'checks': checks, 'frames': rows, 'expected_z_delta': expected_delta}

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.fps = 24
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0))
platform = bpy.context.object
platform.name = 'MovingSupport'
platform.scale = (4, 4, .5)
platform.keyframe_insert(data_path='location', frame=1)
platform.keyframe_insert(data_path='location', frame=10)
platform.location.z = -2
platform.keyframe_insert(data_path='location', frame=11)
platform.keyframe_insert(data_path='location', frame=16)
scene.frame_set(1)
obj = point_object('RestingStack', [(0, 0, .45), (0, 0, .85), (0, 0, 1.25)])
req = request(obj, substeps=4)
req['collider_object'] = platform.name
result = execute_request(req)
assert result['ok'], result
rows = {}
for frame in range(1, 17):
    scene.frame_set(frame)
    rows[str(frame)] = state(obj)
checks = {'rest_before_move': all(abs(p[2] - z) < 1e-4 for p, z in zip(rows['10']['positions'], (.45, .85, 1.25))),
          'wake_after_move': not any(rows['12']['sleep']),
          'fall_after_move': all(b[2] < a[2] - 1e-3 for a, b in zip(rows['10']['positions'], rows['12']['positions'])),
          'downward_velocity': all(v[2] < -.01 for v in rows['12']['velocity']),
          'uniform_stack_gravity': max(v[2] for v in rows['12']['velocity']) - min(v[2] for v in rows['12']['velocity']) < 1e-4}
records['moving_support'] = {'checks': checks, 'frames': rows}
ok = all(all(item['checks'].values()) for item in records.values())
(out / 'results.json').write_text(json.dumps({'ok': ok, 'records': records}, indent=2), encoding='utf-8')
assert ok, {key: row['checks'] for key, row in records.items()}
print('SLEEP_BOUNDARIES_PASS')
