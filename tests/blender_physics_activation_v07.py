"""Public whole-workflow activation, offset start, real output and cold replay."""
import bpy,json,sys,math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import bake,contract,subgroups
mode,folder=sys.argv[sys.argv.index('--')+1:];out=Path(folder).resolve()

def snapshot(obj):
 bpy.context.view_layer.update()
 geo=obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry()
 points=geo.instances_pointcloud();assert points is not None
 transforms=points.attributes['instance_transform']
 return {'positions':[[round(float(x),6) for x in (obj.matrix_world @ item.value).translation] for item in transforms.data],
         'velocity':[[round(float(x),6) for x in item.vector] for item in points.attributes['pnw_velocity'].data],
         'rotation':[[round(float(x),6) for x in item.vector] for item in points.attributes['pnw_rotation'].data],
         'active':[bool(item.value) for item in points.attributes['pnw_active'].data]}

def valid(obj,request):
 mod=obj.modifiers['PhysicsNodes'];core=mod.node_group.nodes['PNW_CoreNode'].node_tree
 return bake.is_cache_valid(obj,mod,contract.normalize_request(request)['parameters_hash'],subgroups.tree_fingerprint(core))[:2]

if mode=='build':
 out.mkdir(parents=True,exist_ok=False);bpy.ops.wm.read_factory_settings(use_empty=True);addon.register()
 scene=bpy.context.scene;scene.frame_start=5;scene.frame_end=20;scene.render.fps=30;scene.render.fps_base=1.25;scene.frame_set(5)
 parameters={'frame_start':5,'frame_end':20,'scatter_count':0,'substeps':4,'stiffness':0,'rest_length':0,
             'ground_z':-100,'wind_turbulence':0,'wind_vector':[0,0,0],'drag':0}
 activation={'Immediate':0,'Delayed':10,'Early':3,'AtStart':5,'Never':30};requests={}
 for name,release in activation.items():
  mesh=bpy.data.meshes.new(name);mesh.from_pydata([(0,0,5),(3,0,6)],[],[])
  obj=bpy.data.objects.new(name,mesh);scene.collection.objects.link(obj)
  obj.rotation_euler=(.1,.2,.3);obj.scale=(-1.2,.8,1.3);obj.location=(1,2,4)
  requests[name]={'target_object':name,'preset_id':'rigid_drop_pack','bake':True,
                  'bake_directory':str(out/name),'parameters':{**parameters,'activation_frame':release}}
 bpy.context.view_layer.update();bpy.ops.wm.save_as_mainfile(filepath=str(out/'input.blend'))
 for name,request in requests.items():
  result=addon.execute_request(request);assert result['ok'] and result['bake']['baked'],result
  assert valid(bpy.data.objects[name],request)[0],valid(bpy.data.objects[name],request)
 records={name:{} for name in requests}
 for frame in range(5,21):
  scene.frame_set(frame)
  for name in requests:records[name][str(frame)]=snapshot(bpy.data.objects[name])
 for name,release in activation.items():
  for frame in range(5,21):
   row=records[name][str(frame)]
   assert row['active']==[release<=frame]*2,(name,frame,row)
   if frame<release:
    assert row==records[name]['5'],(name,frame,'waiting state changed')
    assert row['velocity']==[[0,0,0]]*2
 assert records['Early']==records['Immediate']
 assert records['AtStart']==records['Immediate']
 for frame in range(10,21):
  assert records['Delayed'][str(frame)]==records['Immediate'][str(frame-4)],(frame,records['Delayed'][str(frame)],records['Immediate'][str(frame-4)])
 assert records['Delayed']['10']['positions'][0][2]<records['Delayed']['9']['positions'][0][2]
 bpy.ops.wm.save_as_mainfile(filepath=str(out/'baked.blend'))
 # Actual socket change, independent from request hash, invalidates old cache.
 obj=bpy.data.objects['Delayed'];mod=obj.modifiers['PhysicsNodes']
 socket=next(s for s in mod.node_group.interface.items_tree if s.item_type=='SOCKET' and s.in_out=='INPUT' and s.name=='Activation Frame')
 prop=getattr(mod.properties.inputs,socket.identifier);prop.value=11;obj.update_tag();bpy.context.view_layer.update()
 changed=valid(obj,requests['Delayed']);assert not changed[0],changed
 prop.value=10;obj.update_tag();bpy.context.view_layer.update();assert valid(obj,requests['Delayed'])[0]
 (out/'build.json').write_text(json.dumps({'ok':True,'requests':requests,'frames':records,'changed_socket':changed},indent=2),encoding='utf-8')
elif mode=='cold':
 expected=json.loads((out/'build.json').read_text(encoding='utf-8'))
 bpy.ops.wm.open_mainfile(filepath=str(out/'baked.blend'));rows={}
 for name,request in expected['requests'].items():
  obj=bpy.data.objects[name];assert valid(obj,request)[0],valid(obj,request)
  rows[name]={}
  for frame in [20,5,9,10,11,20]:
   bpy.context.scene.frame_set(frame);actual=snapshot(obj)
   assert actual==expected['frames'][name][str(frame)],(name,frame)
   rows[name][str(frame)]=actual
 (out/'cold.json').write_text(json.dumps({'ok':True,'frames':rows},indent=2),encoding='utf-8')
else:raise ValueError(mode)
print('ACTIVATION_'+mode.upper()+'_PASS')
