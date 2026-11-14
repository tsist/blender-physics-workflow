"""Real output attributes, prototype-shadowing and unchanged-geometry regressions."""
from pathlib import Path
import copy
import json
import sys
import bpy
from mathutils import Matrix

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
sys.path.insert(0,str(ROOT/'tools/physics_workflow_cli_v1'))
from physics_nodes_workflow_addon import geometry_evidence as evidence, attribute_evidence, inspection
from physics_workflow_cli_v1 import physics_verifier
OUT=Path(sys.argv[sys.argv.index('--')+1]).resolve()
OUT.mkdir(parents=True,exist_ok=False)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.preferences.filepaths.file_preview_type='NONE'
bpy.context.preferences.filepaths.save_version=0

def target(name):
    mesh=bpy.data.meshes.new(name)
    mesh.from_pydata([(0,0,0),(2,0,0),(0,1,0)],[],[(0,1,2)])
    obj=bpy.data.objects.new(name,mesh);bpy.context.scene.collection.objects.link(obj)
    return obj

def sample(obj):
    obj.data.update();obj.update_tag();bpy.context.view_layer.update()
    return evidence.sample_frame(obj,1)

obj=target('StateOnly')
for dtype in attribute_evidence.TYPE_FIELDS:
    attr=obj.data.attributes.new('pnw_'+dtype,dtype,'POINT')
    if dtype=='STRING':attr.data[0].value=b'hello\xff'
    if dtype=='FLOAT4X4':attr.data[0].value=Matrix.Identity(4)
vel=obj.data.attributes.new('pnw_velocity','FLOAT_VECTOR','POINT')
for i,item in enumerate(vel.data):item.vector=(i+1,0,0)
before=sample(obj)
vel=obj.data.attributes['pnw_velocity'];vel.data[1].vector=(17,0,0)
after=sample(obj)
assert before['geometry_sha256']==after['geometry_sha256']
assert before['physics_attributes_sha256']!=after['physics_attributes_sha256']
assert before['state_sha256']!=after['state_sha256']
assert len(after['physics_attributes']['attributes'])==len(attribute_evidence.TYPE_FIELDS)+1
checks, failures=physics_verifier._compare_summary(after,before,1)
assert failures and all(c['passed'] for c in checks if c['field']=='geometry_sha256')
old=copy.deepcopy(after);old['evidence_schema']='physics-geometry-evidence-v1'
old.pop('state_sha256');old.pop('physics_attributes_sha256')
_,old_failures=physics_verifier._compare_summary(after,old,1)
assert 'EVIDENCE_SCHEMA_MISMATCH' in old_failures and 'INVALID_EXPECTED_DIGEST' in old_failures

# Preserve independently readable evidence whose only change is attribute data.
blend=OUT/'changed-attributes.blend'
bpy.ops.wm.save_as_mainfile(filepath=str(blend))
spec={'output_blend':str(blend),'target_object':obj.name,'require_cache':False,
      'cache':{'required':False},'frame_start':1,'frame_end':1,'expected_frames':{'1':before}}
(OUT/'changed-spec.json').write_text(json.dumps(spec),encoding='utf-8')
spec['expected_frames']={'1':after}
(OUT/'valid-spec.json').write_text(json.dumps(spec),encoding='utf-8')
spec['expected_frames']={'1':old}
(OUT/'legacy-spec.json').write_text(json.dumps(spec),encoding='utf-8')

vel=obj.data.attributes['pnw_velocity'];vel.data[0].vector=(float('nan'),0,0)
try:sample(obj)
except evidence.GeometryEvidenceError as exc:assert exc.code=='NON_FINITE_PHYSICS_ATTRIBUTE'
else:raise AssertionError('nonfinite physics attribute was accepted')
vel.data[0].vector=(1,0,0)

# Instance attributes must not be replaced by the prototype's same-named field.
inst=target('Instances')
v=inst.data.attributes.new('pnw_velocity','FLOAT_VECTOR','POINT')
for i,item in enumerate(v.data):item.vector=(i+1,0,0)
tree=bpy.data.node_groups.new('InstancesEvidence','GeometryNodeTree')
tree.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
tree.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
gin=tree.nodes.new('NodeGroupInput');gout=tree.nodes.new('NodeGroupOutput')
cube=tree.nodes.new('GeometryNodeMeshCube')
store=tree.nodes.new('GeometryNodeStoreNamedAttribute');store.data_type='FLOAT_VECTOR';store.domain='POINT'
store.inputs['Name'].default_value='pnw_velocity';store.inputs['Value'].default_value=(99,0,0)
node=tree.nodes.new('GeometryNodeInstanceOnPoints')
tree.links.new(gin.outputs['Geometry'],node.inputs['Points'])
tree.links.new(cube.outputs['Mesh'],store.inputs['Geometry'])
tree.links.new(store.outputs['Geometry'],node.inputs['Instance'])
tree.links.new(node.outputs['Instances'],gout.inputs['Geometry'])
mod=inst.modifiers.new('Evidence','NODES');mod.node_group=tree
si=sample(inst)
stats=si['physics_attributes']['velocity_statistics']
assert stats['count']==3 and stats['mean']==2 and stats['max']==3,stats
attrs=si['physics_attributes']['attributes']
assert attrs[0]['component']=='instances' and attrs[0]['domain']=='INSTANCE'
inst.data.vertices[0].co,inst.data.vertices[1].co=inst.data.vertices[1].co.copy(),inst.data.vertices[0].co.copy()
swapped=sample(inst)
assert si['geometry_sha256']==swapped['geometry_sha256']
assert si['physics_attributes_sha256']!=swapped['physics_attributes_sha256']
diagnostic=inspection.inspect_workflow(inst,None,frame_start=1,frame_end=1)
assert diagnostic['speed_source']=='evaluated_output_pnw_velocity' and diagnostic['mean_speed']==2
assert diagnostic['diagnostics_valid']
plain=sample(target('NoState'))
assert not plain['state_coverage']['physics_attributes_compared']
assert plain['state_coverage']['availability']=='no_pnw_attributes_in_output'
for coverage_key in ('physics_attributes_compared','mesh_geometry','instance_geometry'):
    forged=copy.deepcopy(plain);forged['state_coverage'][coverage_key]=not plain['state_coverage'][coverage_key]
    assert physics_verifier._compare_summary(plain,forged,1)[1]
forged=copy.deepcopy(plain);forged['physics_attributes']['scope']='all_internal_solver_state'
assert physics_verifier._compare_summary(plain,forged,1)[1]

# Co-located instances retain the same geometry multiset when prototypes swap,
# but the velocity-to-shape assignment must still change the state evidence.
for vertex in inst.data.vertices:vertex.co=(0,0,0)
cube2=tree.nodes.new('GeometryNodeMeshCube');cube2.inputs['Size'].default_value=(2,2,2)
refs=tree.nodes.new('GeometryNodeGeometryToInstance')
tree.links.new(cube.outputs['Mesh'],refs.inputs['Geometry'])
tree.links.new(cube2.outputs['Mesh'],refs.inputs['Geometry'])
tree.links.new(refs.outputs['Instances'],node.inputs['Instance'])
idx=tree.nodes.new('GeometryNodeInputIndex');tree.links.new(idx.outputs['Index'],node.inputs['Instance Index'])
node.inputs['Pick Instance'].default_value=True
# Three points use 0,1,0; make all three distinct rows select the same-count
# pair by retaining only two vertices in this independent fixture.
newmesh=bpy.data.meshes.new('ReferenceBinding');newmesh.from_pydata([(0,0,0),(0,0,0)],[],[])
v=newmesh.attributes.new('pnw_velocity','FLOAT_VECTOR','POINT')
v.data[0].vector=(1,0,0);v.data[1].vector=(2,0,0)
inst.data=newmesh
binding_before=sample(inst)
cube.inputs['Size'].default_value=(2,2,2);cube2.inputs['Size'].default_value=(1,1,1)
binding_after=sample(inst)
assert binding_before['geometry_sha256']==binding_after['geometry_sha256']
assert binding_before['physics_attributes_sha256']!=binding_after['physics_attributes_sha256']

payload={'pass':True,'schema':evidence.EVIDENCE_SCHEMA,'types':list(attribute_evidence.TYPE_FIELDS),
         'geometry_unchanged_state_changed':True,'instance_velocity_statistics':stats,
         'instance_assignment_swap_detected':True,'legacy_rejected':old_failures,
         'prototype_assignment_swap_detected':True,'forged_coverage_rejected':True,
         'nonfinite_rejected':True,'source_geometry_retained':True,
         'scope':'Top-level evaluated output attributes; internal and nested reference state excluded'}
(OUT/'result.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
print('ATTRIBUTE_EVIDENCE_V04_PASS')
