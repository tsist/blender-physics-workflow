"""Waiting must bypass contact/chain correction, not just force integration."""
import bpy,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
out=Path(sys.argv[sys.argv.index('--')+1]);out.mkdir(parents=True,exist_ok=False)
rows={}
for case,points,parameters in [('overlap',[(0,0,-.1)],{'ground_z':0,'particle_radius':.2,'stiffness':0}),
                             ('tether',[(0,0,3),(0,0,4)],{'ground_z':-10,'anchor_pos':[0,0,0],'stiffness':1,'rest_length':.5})]:
 bpy.ops.wm.read_factory_settings(use_empty=True)
 mesh=bpy.data.meshes.new(case);mesh.from_pydata(points,[],[])
 obj=bpy.data.objects.new(case,mesh);bpy.context.scene.collection.objects.link(obj)
 req={'target_object':obj.name,'parameters':{'frame_end':8,'activation_frame':6,'scatter_count':0,'wind_turbulence':0,**parameters}}
 res=addon.execute_request(req);assert res['ok'],res
 data={}
 for frame in range(1,9):
  bpy.context.scene.frame_set(frame);bpy.context.view_layer.update()
  geo=obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry();p=geo.instances_pointcloud()
  data[str(frame)]={'z':[round(i.value.translation.z,6) for i in p.attributes['instance_transform'].data],
                    'active':[bool(i.value) for i in p.attributes['pnw_active'].data]}
 assert all(data[str(f)]==data['1'] for f in range(1,6)),(case,data)
 assert data['6']['z']!=data['5']['z'] and all(data['6']['active']),(case,data)
 if case=='overlap':assert min(data['6']['z'])>=.19999,data
 rows[case]=data
(out/'result.json').write_text(json.dumps({'ok':True,'cases':rows},indent=2),encoding='utf-8')
print('ACTIVATION_CONTACTS_PASS')
