"""No-output preflight refusals, scoped rollback, and authored graph protection."""
import bpy,sys,json
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import identity,subgroups
args=sys.argv[sys.argv.index('--')+1:];out=Path(args[0]);source=Path(args[1]);out.mkdir(parents=True,exist_ok=False)
bpy.ops.wm.open_mainfile(filepath=str(source/'prepared-only.blend'));addon.register()
target=bpy.data.objects['Target'];coll=bpy.data.collections['OriginalPrototypes'];original=bpy.data.objects['OriginalB']
request={'operation':'prepare_instances','target_object':target.name,'preset_id':'rigid_drop_pack','instance_collection':coll.name}
def ids():return {'objects':set(bpy.data.objects.keys()),'meshes':set(bpy.data.meshes.keys()),'collections':set(bpy.data.collections.keys()),'node_groups':set(bpy.data.node_groups.keys())}
def rejected(label):
 before=ids();r=addon.execute_request(request);assert not r['ok'],r;assert ids()==before,label
 results[label]=r['error']['message']
results={}
parent=bpy.data.objects.new('Parent',None);original.parent=parent
rejected('parent');original.parent=None
modifier=original.modifiers.new('AuthoredModifier','BEVEL');rejected('modifier');original.modifiers.remove(modifier)
original.keyframe_insert('location',frame=1);rejected('animation');original.animation_data_clear()
constraint=original.constraints.new('LIMIT_LOCATION');rejected('constraint');original.constraints.remove(constraint)
child=bpy.data.collections.new('Child');coll.children.link(child);rejected('nested_collection');coll.children.unlink(child)
original.data.shape_keys if original.data.shape_keys else None
original.shape_key_add(name='Basis');rejected('shape_key');original.shape_key_clear()
old=original.location.x;original.location.x=float('nan');rejected('nonfinite_transform');original.location.x=old
original['pnw_cache_owner_binding__PhysicsNodes']='source-token';original['pnw_state_json__PhysicsNodes']='source-state';original['user_owned_note']='keep'
original['pnw_state_json']='legacy-source-state'
real_hash=identity.base_geometry_sha256
def fail_on_second_object(obj):
 if obj==original:raise RuntimeError('injected-second-copy-failure')
 return real_hash(obj)
before=ids()
with patch.object(identity,'base_geometry_sha256',side_effect=fail_on_second_object):
 r=addon.execute_request(request)
assert not r['ok'] and 'injected-second-copy-failure' in r['error']['message'] and ids()==before
results['rollback']=True
# Creation must not take ownership of source workflow/cache tags.
r=addon.execute_request(request);assert r['ok'],r
prepared=bpy.data.collections[r['prepared_instances']['prepared_collection']]
second=sorted(prepared.objects,key=lambda o:o.name)[1]
assert second.get('user_owned_note')=='keep' and 'pnw_cache_owner_binding__PhysicsNodes' not in second
assert 'pnw_state_json__PhysicsNodes' not in second and original['pnw_cache_owner_binding__PhysicsNodes']=='source-token'
assert 'pnw_state_json' not in second and original['pnw_state_json']=='legacy-source-state'
results['copied_ownership_removed_source_retained']=True
# Preparing a different collection cannot silently repair/rebind an authored graph.
build={**request,'operation':'build','instance_collection':prepared.name,'parameters':{'frame_end':2,'scatter_count':0}}
assert addon.execute_request(build)['ok']
wrapper=target.modifiers['PhysicsNodes'].node_group
gravity=next(l for l in wrapper.links if l.to_node.name=='PNW_CoreNode' and l.to_socket.name=='Gravity')
wrapper.links.remove(gravity);wrapper.nodes.new('ShaderNodeValue').name='KeepAuthored'
before_fingerprint=subgroups.tree_fingerprint(wrapper)
active=bpy.context.view_layer.objects.active;selected=list(bpy.context.selected_objects);frame=(bpy.context.scene.frame_current,bpy.context.scene.frame_subframe)
r=addon.execute_request(request);assert r['ok'],r
assert subgroups.tree_fingerprint(wrapper)==before_fingerprint and 'KeepAuthored' in wrapper.nodes
assert bpy.context.view_layer.objects.active==active and list(bpy.context.selected_objects)==selected
assert (bpy.context.scene.frame_current,bpy.context.scene.frame_subframe)==frame
results['authored_graph_and_context_preserved']=True
(out/'result.json').write_text(json.dumps({'ok':True,'cases':results},indent=2),encoding='utf-8');print(json.dumps(results))
