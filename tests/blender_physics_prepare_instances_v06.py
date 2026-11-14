"""Public prototype preparation, persistence, geometry/material output checks."""
import bpy,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import identity,export,contract,core
args=sys.argv[sys.argv.index('--')+1:];mode=args[0];out=Path(args[1]).resolve()

def write(name,data): (out/name).write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
def snap(obj):
 return {'mesh':obj.data.name,'geometry':identity.base_geometry_sha256(obj),
         'materials':[(s.link,s.material.name if s.material else None) for s in obj.material_slots],
         'basis':[[float(x) for x in row] for row in obj.matrix_basis],
         'delta':list(obj.delta_location),'groups':list(obj.vertex_groups.keys()),
         'uv':[(u.name,u.active_render) for u in obj.data.uv_layers]}
def canonical(value):return json.loads(json.dumps(value))
def realized(obj):
 m=export._realized_mesh(obj)
 try:
  return {'vertices':len(m.vertices),'faces':len(m.polygons),
    'material_faces':{mat.name:sum(m.materials[p.material_index]==mat for p in m.polygons) for mat in m.materials if mat},
    'uvs':[u.name for u in m.uv_layers],
    'world_vertices':[[round(float(x),6) for x in obj.matrix_world @ v.co] for v in m.vertices]}
 finally:bpy.data.meshes.remove(m)

if mode=='produce':
 out.mkdir(parents=True,exist_ok=False);bpy.ops.wm.read_factory_settings(use_empty=True);addon.register()
 coll=bpy.data.collections.new('OriginalPrototypes');coll.instance_offset=(.3,.1,-.2)
 bpy.ops.mesh.primitive_cube_add();a=bpy.context.object;a.name='OriginalA'
 for c in list(a.users_collection):c.objects.unlink(a)
 coll.objects.link(a)
 base=bpy.data.materials.new('OriginalData');a.data.materials.append(base);a.data.materials.append(None)
 a.data.uv_layers.new(name='SecondUV');a.data.uv_layers[1].active_render=True
 a.vertex_groups.new(name='PreservedWeights').add([0,1],.7,'REPLACE')
 b=a.copy();b.name='OriginalB';coll.objects.link(b)
 for i,(o,color) in enumerate([(a,'Blue'),(b,'Green')]):
  o.material_slots[0].link='OBJECT';o.material_slots[0].material=bpy.data.materials.new(color)
  o.material_slots[0].material.diffuse_color=(.04,.2,.9,1) if color=='Blue' else (.04,.7,.25,1)
  o.location=(i*2,.3,1);o.rotation_euler=(.1,.2,.3);o.scale=(-1.3,.8,1.2);o.delta_location=(.2,0,.1)
 mesh=bpy.data.meshes.new('Points');mesh.from_pydata([(i*4,0,3) for i in range(6)],[],[])
 target=bpy.data.objects.new('Target',mesh);bpy.context.scene.collection.objects.link(target)
 target.select_set(True);bpy.context.view_layer.objects.active=target;bpy.context.view_layer.update()
 bpy.context.scene.frame_set(7,subframe=.25)
 before=canonical([snap(a),snap(b)]);object_count=len(bpy.data.objects)
 req={'operation':'prepare_instances','target_object':target.name,'preset_id':'rigid_drop_pack','instance_collection':coll.name,'parameters':{'frame_start':1,'frame_end':3,'scatter_count':0}}
 result=addon.execute_request(req);assert result['ok'],result
 preparation=result['prepared_instances']
 prepared=bpy.data.collections[result['prepared_instances']['prepared_collection']]
 assert prepared.use_fake_user
 assert len(bpy.data.objects)==object_count+2 and a.data==b.data
 assert canonical([snap(a),snap(b)])==before
 for original,copy in zip([a,b],sorted(prepared.objects,key=lambda x:x.name)):
  assert copy.data!=original.data and copy.matrix_world==original.matrix_world
  assert all(s.link=='DATA' for s in copy.material_slots)
  assert [s.material for s in copy.material_slots]==[s.material for s in original.material_slots]
  assert list(copy.vertex_groups.keys())==list(original.vertex_groups.keys())
 assert list(prepared.instance_offset)==list(coll.instance_offset)
 assert [bpy.context.scene.frame_current,bpy.context.scene.frame_subframe]==[7,.25]
 bpy.ops.wm.save_as_mainfile(filepath=str(out/'prepared-only.blend'))
 # UI preparation selects a new snapshot but does not bind or alter a graph.
 target.pnw_settings.instance_collection=coll
 assert bpy.ops.pnw.prepare_instances()=={'FINISHED'}
 assert target.pnw_settings.instance_collection not in {coll,prepared} and not target.modifiers
 assert 'shaders remain shared' in target.pnw_settings.last_instance_summary
 rows={}
 for normalize in [False,True]:
  build={**req,'operation':'build' if not rows else 'parameters','instance_collection':prepared.name,
         'parameters':{'frame_start':1,'frame_end':3,'scatter_count':0,'instance_normalize':normalize,'particle_radius':.6}}
  result=addon.execute_request(build);assert result['ok'],result
  bpy.context.scene.frame_set(1);values=realized(target)
  assert set(values['material_faces'])=={'Blue','Green'},values
  assert values['material_faces']=={'Blue':18,'Green':18},values
  exp=addon.execute_request({**build,'operation':'export','export_format':'STATIC_MESH','export_path':str(out/f'static-{normalize}.blend'),'export_frame':1})
  assert exp['ok'],exp
  rows[str(normalize)]={'realized':values,'static':exp['export']}
 # Snapshot is independent of source geometry/slot edits; shared shaders are intentional.
 copies=sorted(prepared.objects,key=lambda x:x.name);clone_before=canonical([snap(o) for o in copies])
 a.data.vertices[0].co.x+=2;a.material_slots[0].material=bpy.data.materials.new('SourceLater')
 assert canonical([snap(o) for o in copies])==clone_before
 a.data.vertices[0].co.x-=2;a.material_slots[0].material=bpy.data.materials['Blue']
 assert canonical([snap(a),snap(b)])==before
 write('result.json',{'ok':True,'prepared':prepared.name,'preparation':preparation,
                     'source':before,'copies':clone_before,'cases':rows,'ui_summary':target.pnw_settings.last_instance_summary})
elif mode=='cold':
 expected=json.loads((out/'result.json').read_text(encoding='utf-8'))
 bpy.ops.wm.open_mainfile(filepath=str(out/'prepared-only.blend'))
 c=bpy.data.collections[expected['prepared']];copies=sorted(c.objects,key=lambda x:x.name)
 actual=canonical([snap(o) for o in copies]);write('cold-observed.json',actual)
 differences=[{'object':i,'field':k,'actual':v,'expected':expected['copies'][i].get(k)} for i,item in enumerate(actual) for k,v in item.items() if v!=expected['copies'][i].get(k)]
 assert not differences,differences
 assert c.use_fake_user and len(c.objects)==2
 assert bpy.data.objects['OriginalA'].data==bpy.data.objects['OriginalB'].data
 assert canonical([snap(bpy.data.objects[n]) for n in ['OriginalA','OriginalB']])==expected['source']
 target=bpy.data.objects['Target']
 for normalize in [False,True]:
  req={'operation':'build' if not target.modifiers else 'parameters','target_object':'Target',
       'preset_id':'rigid_drop_pack','instance_collection':c.name,
       'parameters':{'frame_start':1,'frame_end':3,'scatter_count':0,'instance_normalize':normalize,'particle_radius':.6}}
  res=addon.execute_request(req);assert res['ok'],res
  bpy.context.scene.frame_set(1)
  assert realized(target)==expected['cases'][str(normalize)]['realized']
 for normalize,row in expected['cases'].items():
  bpy.ops.wm.open_mainfile(filepath=row['static']['saved_path'])
  obj=bpy.data.objects[row['static']['static_object_name']]
  assert realized(obj)==row['realized']
 write('cold.json',{'ok':True,'snapshot_survives_without_build':True,'static_both_modes':True})
