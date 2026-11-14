"""Observe cache validity of the actual prepared input, not provenance source."""
import bpy,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from physics_nodes_workflow_addon import bake,contract,subgroups,export
args=sys.argv[sys.argv.index('--')+1:];folder=Path(args[0]);info=json.loads((folder/'result.json').read_text(encoding='utf-8'))
bpy.ops.wm.open_mainfile(filepath=info['baked_blend'])
obj=bpy.data.objects['Target'];mod=obj.modifiers['PhysicsNodes'];req=contract.normalize_request(info['request'])
def valid():
 return bake.is_cache_valid(obj,mod,req['parameters_hash'],subgroups.tree_fingerprint(mod.node_group.nodes['PNW_CoreNode'].node_tree))[:2]
assert valid()[0],valid()
source=bpy.data.collections['OriginalPrototypes'];prepared=bpy.data.collections[info['prepared_collection']]
o=sorted(source.objects,key=lambda x:x.name)[0];p=sorted(prepared.objects,key=lambda x:x.name)[0]
before=o.data.vertices[0].co.copy();mat=o.material_slots[0].material
o.data.vertices[0].co.x+=1;o.material_slots[0].material=bpy.data.materials.new('SourceChanged')
bpy.context.view_layer.update();source_changed=valid();assert source_changed[0],source_changed
o.data.vertices[0].co=before;o.material_slots[0].material=mat
before=p.data.vertices[0].co.copy();p.data.vertices[0].co.x+=.5
bpy.context.view_layer.update();geometry_changed=valid();assert not geometry_changed[0],geometry_changed
p.data.vertices[0].co=before;bpy.context.view_layer.update();assert valid()[0],valid()
mat=p.data.materials[0];p.data.materials[0]=bpy.data.materials.new('SnapshotChanged')
bpy.context.view_layer.update();slots_changed=valid();assert not slots_changed[0],slots_changed
p.data.materials[0]=mat;bpy.context.view_layer.update();assert valid()[0]
p.material_slots[0].link='OBJECT';p.material_slots[0].material=bpy.data.materials.new('SnapshotObjectOverride')
bpy.context.view_layer.update();override_changed=valid();assert not override_changed[0],override_changed
p.material_slots[0].link='DATA';bpy.context.view_layer.update();assert valid()[0]
# A prepared marker cannot bypass the raw export's current-material guard.
p.material_slots[0].link='OBJECT';p.material_slots[0].material=bpy.data.materials['SnapshotObjectOverride']
try:export.export_fidelity.check_prototype_materials(obj,req)
except ValueError as exc:guard=str(exc)
else:raise AssertionError('prepared marker bypassed current object override guard')
p.material_slots[0].link='DATA';bpy.context.view_layer.update()
color=mat.diffuse_color[:];mat.diffuse_color=(.7,.1,.2,1)
shader_changed=valid();assert shader_changed[0],shader_changed
mat.diffuse_color=color
result={'ok':True,'source_edit_keeps_snapshot_cache':source_changed,'snapshot_geometry_invalidates':geometry_changed,
        'snapshot_data_slot_invalidates':slots_changed,'snapshot_object_slot_invalidates':override_changed,
        'shared_shader_tuning_is_not_geometry_identity':shader_changed,'marker_does_not_bypass_guard':guard}
(folder/'cache-boundaries.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
