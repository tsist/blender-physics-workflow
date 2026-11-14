"""Real Blender regressions for visible geometry and unavailable motion diagnostics.

Run with --background --factory-startup --python-exit-code 1 --python FILE
-- baseline|verify ADDON_PARENT NEW_OUTPUT_DIR. No user scene or preferences used.
"""
from pathlib import Path
import json
import math
import sys

import bpy

mode, addon_parent, output = sys.argv[sys.argv.index('--') + 1:]
sys.path.insert(0, str(Path(addon_parent).resolve()))
from physics_nodes_workflow_addon import inspection

out = Path(output).resolve()
out.mkdir(parents=True, exist_ok=False)
bpy.ops.wm.read_factory_settings(use_empty=True)


def mesh_object(name, points):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(points, [], [])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def inspect(obj, start=1, end=6):
    obj.data.update()
    obj.update_tag()
    bpy.context.view_layer.update()
    return inspection.inspect_workflow(obj, None, start, end)


empty = mesh_object('Empty', [])
empty_result = inspect(empty)
changing = mesh_object('ChangingCount', [(0, 0, 1)])
second = bpy.data.meshes.new('SecondCount')
second.from_pydata([(0, 0, 1), (0, 0, 2)], [], [])
first = changing.data


def change_count(scene):
    changing.data = first if scene.frame_current < 4 else second


bpy.app.handlers.frame_change_pre.append(change_count)
try:
    changing_result = inspect(changing)
finally:
    bpy.app.handlers.frame_change_pre.remove(change_count)

single = mesh_object('SingleFrame', [(0, 0, 1)])
single_result = inspect(single, 1, 1)

instances = mesh_object('CubeInstances', [(0, 0, .5), (3, 0, 1.5)])
tree = bpy.data.node_groups.new('DiagnosticInstances', 'GeometryNodeTree')
tree.interface.new_socket(name='Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
gin = tree.nodes.new('NodeGroupInput')
gout = tree.nodes.new('NodeGroupOutput')
cube = tree.nodes.new('GeometryNodeMeshCube')
cube.inputs['Size'].default_value = (2, 2, 2)
node = tree.nodes.new('GeometryNodeInstanceOnPoints')
tree.links.new(gin.outputs['Geometry'], node.inputs['Points'])
tree.links.new(cube.outputs['Mesh'], node.inputs['Instance'])
tree.links.new(node.outputs['Instances'], gout.inputs['Geometry'])
mod = instances.modifiers.new('DiagnosticInstances', 'NODES')
mod.node_group = tree
instance_result = inspect(instances)

results = {'empty': empty_result, 'changing_count': changing_result,
           'single_frame': single_result, 'instances': instance_result}
if mode == 'baseline':
    assert empty_result['resting_ratio'] == 1.0
    assert changing_result['resting_ratio'] == 1.0
    assert single_result['resting_ratio'] == 1.0
    assert instance_result['frames']['6']['ground_clearance_min'] == .5
    results['reproduced'] = ['empty_false_rest', 'changing_count_false_rest',
                             'single_frame_false_rest', 'centers_mislabelled_clearance']
else:
    for name in ('empty', 'changing_count', 'single_frame'):
        result = results[name]
        assert result['resting_ratio'] is None, (name, result['resting_ratio'])
        assert result['mean_speed'] is None and result['max_speed'] is None
        assert result['speed_source'] == 'not_observable'
        assert result['late_motion']['status'] == 'not_observable'
    ground = instance_result['frames']['6']['ground_contact']
    assert ground['visible_geometry']['status'] == 'observed', ground
    assert abs(ground['visible_geometry']['clearance_min'] + .5) < 1e-6, ground
    assert ground['instance_origins']['clearance_min'] == .5
    assert ground['visible_geometry']['below_ground_vertex_count'] == 4
    # Mixed mesh plus nested instances, with negative scale and rotation.
    nested = tree.nodes.new('GeometryNodeGeometryToInstance')
    tree.links.new(node.outputs['Instances'], nested.inputs['Geometry'])
    join = tree.nodes.new('GeometryNodeJoinGeometry')
    tree.links.new(nested.outputs['Instances'], join.inputs['Geometry'])
    tree.links.new(gin.outputs['Geometry'], join.inputs['Geometry'])
    tree.links.new(join.outputs['Geometry'], gout.inputs['Geometry'])
    instances.scale = (-1.5, 2, .75)
    instances.rotation_euler.y = math.pi / 4
    mixed = inspect(instances)
    # Independent oracle: realize the complete output temporarily, transform all vertices.
    realize = tree.nodes.new('GeometryNodeRealizeInstances')
    tree.links.new(join.outputs['Geometry'], realize.inputs['Geometry'])
    tree.links.new(realize.outputs['Geometry'], gout.inputs['Geometry'])
    instances.update_tag()
    bpy.context.view_layer.update()
    ev = instances.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = ev.to_mesh()
    expected = min((ev.matrix_world @ v.co).z for v in mesh.vertices)
    expected_count = len(mesh.vertices)
    ev.to_mesh_clear()
    actual = mixed['frames']['6']['ground_contact']['visible_geometry']
    assert actual['status'] == 'observed', actual
    assert abs(actual['clearance_min'] - expected) < 1e-6, (actual, expected)
    assert actual['vertex_count'] == expected_count, (actual, expected_count)
    results['mixed_nested_transformed'] = {'measurement': actual, 'realized_oracle': expected}
    # Low-speed proportion is per point, not a threshold applied to the mean.
    moving = mesh_object('MixedMotion', [(0, 0, 1), (0, 0, 2)])
    def move_one(scene):
        moving.data.vertices[1].co.x = (scene.frame_current - 1) * .01
        moving.data.update()
    bpy.app.handlers.frame_change_pre.append(move_one)
    try:
        motion = inspect(moving)
    finally:
        bpy.app.handlers.frame_change_pre.remove(move_one)
    assert motion['resting_ratio'] == .5, motion
    assert motion['speed_source'] == 'estimated_from_sampled_position_delta'
    results['estimated_motion'] = motion
    # Sleep is explicit state, independent of low velocity or contact convergence.
    state = mesh_object('SleepState', [(0, 0, 1), (0, 0, 2)])
    for name, vals in [('pnw_sleep', [True, False]), ('pnw_active', [True, True]),
                       ('pnw_sleep_rooted', [True, False])]:
        attr = state.data.attributes.new(name, 'BOOLEAN', 'POINT')
        for item, value in zip(attr.data, vals):
            item.value = value
    sleep = inspect(state, 1, 1)['frames']['1']['solver_sleep']
    assert sleep['status'] == 'observed' and sleep['sleeping_count'] == 1, sleep
    assert sleep['count'] == 2 and sleep['sleeping_ratio'] == .5
    assert sleep['active_count'] == 2 and sleep['rooted_count'] == 1
    state.data.attributes.remove(state.data.attributes['pnw_sleep'])
    state.data.attributes.new('pnw_sleep', 'FLOAT', 'POINT')
    invalid = inspect(state, 1, 1)['frames']['1']['solver_sleep']
    assert invalid['status'] == 'invalid_attribute' and invalid['sleeping_count'] is None
    results['sleep_observed'] = sleep
    results['sleep_invalid'] = invalid
    # A non-mesh component is not silently included in mesh-ground claims.
    curve = tree.nodes.new('GeometryNodeCurvePrimitiveLine')
    tree.links.new(curve.outputs['Curve'], gout.inputs['Geometry'])
    unsupported = inspect(instances)['frames']['6']
    assert unsupported['ground_contact']['visible_geometry']['status'] in ('partial', 'not_observable')
    assert unsupported['solver_sleep']['status'] == 'not_observable'
    results['curve_component'] = unsupported
    # Same point count does not make vertices and instance origins comparable.
    def change_source(scene):
        socket = gin.outputs['Geometry'] if scene.frame_current < 4 else node.outputs['Instances']
        tree.links.new(socket, gout.inputs['Geometry'])
        instances.update_tag()
    bpy.app.handlers.frame_change_pre.append(change_source)
    try:
        switched = inspect(instances)
    finally:
        bpy.app.handlers.frame_change_pre.remove(change_source)
    assert switched['late_motion']['reason'] == 'point_source_changed', switched['late_motion']
    assert switched['resting_ratio'] is None
    results['point_source_change'] = switched
    # Malformed velocity is invalid evidence, not permission to estimate from positions.
    invalid_velocity = state.data.attributes.new('pnw_velocity', 'FLOAT', 'POINT')
    wrong_type = inspect(state)
    assert wrong_type['velocity_attribute_status'] == 'invalid_attribute'
    assert wrong_type['mean_speed'] is None and wrong_type['resting_ratio'] is None
    state.data.attributes.remove(invalid_velocity)
    nonfinite_velocity = state.data.attributes.new('pnw_velocity', 'FLOAT_VECTOR', 'POINT')
    nonfinite_velocity.data[0].vector = (float('nan'), 0, 0)
    nonfinite = inspect(state)
    assert nonfinite['velocity_attribute_status'] == 'invalid_attribute'
    assert not nonfinite['diagnostics_valid'] and nonfinite['mean_speed'] is None
    results['wrong_velocity_type'] = wrong_type
    results['nonfinite_velocity'] = nonfinite
    # Contact data uses the same component cardinality and cannot pass from just one field.
    contacts = mesh_object('ContactEvidence', [(0, 0, 1), (1, 0, 1)])
    residual = contacts.data.attributes.new('pnw_contact_residual', 'FLOAT', 'POINT')
    residual.data[1].value = .02
    missing = inspect(contacts)['frames']['6']['contact_convergence']
    assert missing['status'] == 'invalid_attribute' and missing['converged'] is None
    converged = contacts.data.attributes.new('pnw_contact_converged', 'BOOLEAN', 'POINT')
    converged.data[0].value = True
    converged.data[1].value = False
    contact = inspect(contacts)['frames']['6']['contact_convergence']
    assert contact['status'] == 'observed' and contact['converged'] is False and contact['count'] == 2
    assert abs(contact['max_residual'] - .02) < 1e-7
    results['contact_missing'] = missing
    results['contact_observed'] = contact
    # Object prototypes must use evaluated data when their source mesh is empty.
    prototype = mesh_object('GeneratedPrototype', [])
    prototype_tree = bpy.data.node_groups.new('GeneratedPrototypeGeometry', 'GeometryNodeTree')
    prototype_tree.interface.new_socket(name='Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    prototype_out = prototype_tree.nodes.new('NodeGroupOutput')
    prototype_cube = prototype_tree.nodes.new('GeometryNodeMeshCube')
    prototype_cube.inputs['Size'].default_value = (1, 2, 3)
    prototype_tree.links.new(prototype_cube.outputs['Mesh'], prototype_out.inputs['Geometry'])
    prototype.modifiers.new('Generated', 'NODES').node_group = prototype_tree
    object_info = tree.nodes.new('GeometryNodeObjectInfo')
    object_info.inputs['Object'].default_value = prototype
    object_info.inputs['As Instance'].default_value = True
    tree.links.new(object_info.outputs['Geometry'], node.inputs['Instance'])
    tree.links.new(node.outputs['Instances'], gout.inputs['Geometry'])
    evaluated_prototype = inspect(instances)['frames']['6']['ground_contact']['visible_geometry']
    tree.links.new(node.outputs['Instances'], realize.inputs['Geometry'])
    tree.links.new(realize.outputs['Geometry'], gout.inputs['Geometry'])
    instances.update_tag()
    bpy.context.view_layer.update()
    ev = instances.evaluated_get(bpy.context.evaluated_depsgraph_get())
    realized = ev.to_mesh()
    expected = min((ev.matrix_world @ v.co).z for v in realized.vertices)
    assert evaluated_prototype['vertex_count'] == len(realized.vertices) == 16, evaluated_prototype
    assert abs(evaluated_prototype['clearance_min'] - expected) < 1e-6
    ev.to_mesh_clear()
    results['evaluated_object_prototype'] = {'measurement': evaluated_prototype, 'realized_oracle': expected}
    null_object = bpy.data.objects.new('EmptyObject', None)
    bpy.context.scene.collection.objects.link(null_object)
    empty_object_result = inspection.inspect_workflow(null_object, None, 1, 1)
    assert empty_object_result['resting_ratio'] is None
    assert empty_object_result['frames']['1']['ground_contact']['visible_geometry']['status'] == 'not_observable'
    results['empty_object'] = empty_object_result
    # The real panel operator must display unavailable values instead of a rest pass.
    import physics_nodes_workflow_addon as addon
    addon.register()
    bpy.context.view_layer.objects.active = empty
    empty.select_set(True)
    empty.pnw_settings.frame_end = 6
    assert bpy.ops.pnw.inspect_workflow() == {'FINISHED'}
    summary = empty.pnw_settings.last_inspect_summary
    assert 'Speed: unavailable' in summary and 'Low-speed fraction: unavailable' in summary, summary
    assert 'Solver sleeping: not_observable' in summary, summary
    results['empty_ui_summary'] = summary
    empty.pnw_settings.frame_start = 10
    try:
        bpy.ops.pnw.inspect_workflow()
    except RuntimeError:
        pass  # Blender raises for report({'ERROR'}); the panel still must be cleared.
    assert empty.pnw_settings.last_inspect_summary.startswith('Diagnostics unavailable:'), empty.pnw_settings.last_inspect_summary
    results['failed_ui_summary'] = empty.pnw_settings.last_inspect_summary
    addon.unregister()

(out / 'results.json').write_text(json.dumps(results, indent=2, allow_nan=False), encoding='utf-8')
print('PHYSICS_DIAGNOSTICS_' + mode.upper() + '_PASS')
