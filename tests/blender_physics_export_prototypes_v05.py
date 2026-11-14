"""Guard reproduced prototype material loss; verify explicit DATA-material inputs."""
import bpy, sys, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import export, contract
out=Path(sys.argv[sys.argv.index('--')+1]);out.mkdir(parents=True,exist_ok=False)
bpy.ops.wm.read_factory_settings(use_empty=True)
coll=bpy.data.collections.new('Prototypes')
bpy.ops.mesh.primitive_cube_add()
a=bpy.context.object;a.name='PrototypeA'
for c in list(a.users_collection):c.objects.unlink(a)
coll.objects.link(a)
base=bpy.data.materials.new('Base');a.data.materials.append(base)
b=a.copy();b.name='PrototypeB';coll.objects.link(b)
for o,n in [(a,'Blue'),(b,'Green')]:o.material_slots[0].link='OBJECT';o.material_slots[0].material=bpy.data.materials.new(n)
mesh=bpy.data.meshes.new('Points');mesh.from_pydata([(i*5,0,2) for i in range(12)],[],[])
obj=bpy.data.objects.new('Scatter',mesh);bpy.context.scene.collection.objects.link(obj)
rows={}
def req(normalize):
 return {'target_object':obj.name,'preset_id':'rigid_drop_pack','instance_collection':coll.name,'parameters':{'frame_end':2,'scatter_count':0,'instance_normalize':normalize}}
for normalize in [False,True]:
 r=req(normalize);r['operation']='build' if not rows else 'parameters'
 result=addon.execute_request(r);assert result['ok'],result
 r.update(operation='export',export_format='STATIC_MESH',export_frame=1,export_path=str(out/('guard-'+str(normalize)+'.blend')))
 result=addon.execute_request(r);assert not result['ok'] and 'EXPORT_PROTOTYPE_MATERIAL_UNSUPPORTED' in result['error']['message'],result
 assert not Path(r['export_path']).exists()
 rows['override_'+str(normalize)]={'rejected':True,'error':result['error']['message']}
assert a.data==b.data and a.data.materials[0]==base
# Explicit authoring alternative is prepared in this test, never silently by export.
for o in [a,b]:
 mat=o.material_slots[0].material;o.data=o.data.copy();o.material_slots[0].link='DATA';o.data.materials[0]=mat
for normalize in [False,True]:
 r=req(normalize);r['operation']='parameters';assert addon.execute_request(r)['ok']
 r.update(operation='export',export_format='STATIC_MESH',export_frame=1,export_path=str(out/('data-'+str(normalize)+'.blend')))
 result=addon.execute_request(r);assert result['ok'],result
 bpy.context.scene.frame_set(1);m=export._realized_mesh(obj)
 names={m.materials[p.material_index].name for p in m.polygons};bpy.data.meshes.remove(m)
 assert names=={'Blue','Green'},names
 rows['data_'+str(normalize)]={'path':r['export_path'],'static_object':result['export']['static_object_name'],'materials':sorted(names)}
(out/'result.json').write_text(json.dumps({'ok':True,'cases':rows},indent=2),encoding='utf-8')
print(json.dumps(rows))
