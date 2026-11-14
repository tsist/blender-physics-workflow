"""UI parameter application, authored disconnects and previous-version protection."""
import bpy,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import subgroups,contract,bake
args=sys.argv[sys.argv.index('--')+1:];out=Path(args[0]);source=Path(args[1]);legacy=Path(args[2]);out.mkdir(parents=True,exist_ok=False)
bpy.ops.wm.open_mainfile(filepath=str(source/'input.blend'));addon.register()
obj=bpy.data.objects['Delayed'];bpy.context.view_layer.objects.active=obj;obj.select_set(True)
s=obj.pnw_settings;s.frame_start=5;s.frame_end=16;s.scatter_count=0;s.stiffness=0;s.rest_length=0;s.ground_z=-100
s.wind_vector=(0,0,0);s.wind_turbulence=0;s.activation_frame=10
assert bpy.ops.pnw.build_workflow()=={'FINISHED'}
working=bpy.context.active_object;assert working!=obj and not obj.modifiers and working.pnw_settings.activation_frame==10
s=working.pnw_settings;s.activation_frame=12;s.bake_directory=str(out/'b')
assert bpy.ops.pnw.bake_cache()=={'FINISHED'}
state=json.loads(working['pnw_state_json__PhysicsNodes']);assert state['parameters']['activation_frame']==12
mod=working.modifiers['PhysicsNodes'];wrapper=mod.node_group
def observed(frame):
 bpy.context.scene.frame_set(frame)
 g=working.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry();p=g.instances_pointcloud()
 return [bool(i.value) for i in p.attributes['pnw_active'].data]
assert observed(11)==[False,False] and observed(12)==[True,True]
author=wrapper.nodes.new('ShaderNodeValue');author.name='KeepAuthor';author.outputs[0].default_value=123
link=next(l for l in wrapper.links if l.to_node.name=='PNW_CoreNode' and l.to_socket.name=='Activation Frame')
wrapper.links.remove(link);wrapper.nodes['PNW_CoreNode'].inputs['Activation Frame'].default_value=14
fingerprint=subgroups.tree_fingerprint(wrapper)
s.activation_frame=6
assert bpy.ops.pnw.update_parameters()=={'FINISHED'}
assert subgroups.tree_fingerprint(wrapper)==fingerprint and author.outputs[0].default_value==123
assert bpy.ops.pnw.bake_cache()=={'FINISHED'}
assert observed(13)==[False,False] and observed(14)==[True,True]
# Detached authored values control the solve; the UI cannot silently reconnect.
bpy.ops.wm.save_as_mainfile(filepath=str(out/'authored.blend'))
# Load a genuinely old 0.6 graph, not a current graph with a fabricated tag.
assert not obj.modifiers
bpy.ops.wm.open_mainfile(filepath=str(legacy))
working=bpy.data.objects['Target'];mod=working.modifiers['PhysicsNodes'];wrapper=mod.node_group
assert json.loads(working['pnw_state_json__PhysicsNodes'])['implementation_version']=='0.6.0-dev.1'
assert not any(i.name=='Activation Frame' for i in wrapper.interface.items_tree)
author=wrapper.nodes.new('ShaderNodeValue');author.name='KeepAuthor';author.outputs[0].default_value=123
before=subgroups.tree_fingerprint(wrapper)
req={'target_object':working.name,'parameters':{'activation_frame':6,'frame_end':16,'scatter_count':0}}
r=addon.execute_request(req)
assert not r['ok'] and 'LEGACY_WORKFLOW' in r['error']['message'],r
assert subgroups.tree_fingerprint(wrapper)==before and mod.node_group==wrapper
r=addon.execute_request({**req,'force_rebuild':True});assert r['ok'],r
assert mod.node_group!=wrapper and wrapper.use_fake_user and wrapper.nodes['KeepAuthor'].outputs[0].default_value==123
(out/'result.json').write_text(json.dumps({'ok':True,'source_protected':True,'ui_bake_uses_current_activation':12,
 'authored_activation_controls_solve':14,'authored_link_preserved':True,'legacy_rejected':True,'rebuild_retains_old':wrapper.name},indent=2),encoding='utf-8')
print('ACTIVATION_UI_PASS')
