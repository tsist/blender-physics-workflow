"""Match actual Collection Info prototype ordering before and after preparation."""
import bpy,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import export
out=Path(sys.argv[sys.argv.index('--')+1]);out.mkdir(parents=True,exist_ok=False)
bpy.ops.wm.read_factory_settings(use_empty=True)
coll=bpy.data.collections.new('Names')
names=['中文','a','A10','A2','a02','A','Z.001','Z.010','中文长名字'*60+'10','中文长名字'*60+'2']
for index,name in enumerate(names):
 bpy.ops.mesh.primitive_cube_add();o=bpy.context.object;o.name=name
 for c in list(o.users_collection):c.objects.unlink(o)
 coll.objects.link(o);o.data.materials.append(bpy.data.materials.new(name+'Material'))
mesh=bpy.data.meshes.new('Points');mesh.from_pydata([(i*5,0,3) for i in range(len(names))],[],[])
target=bpy.data.objects.new('Target',mesh);bpy.context.scene.collection.objects.link(target)
req={'target_object':target.name,'preset_id':'rigid_drop_pack','instance_collection':coll.name,'parameters':{'frame_end':2,'scatter_count':0,'instance_normalize':False}}
def values():
 bpy.context.scene.frame_set(1);m=export._realized_mesh(target)
 try:
  d={}
  for p in m.polygons:
   x=sum(m.vertices[i].co.x for i in p.vertices)/len(p.vertices)
   d.setdefault(round(x/5),set()).add(m.materials[p.material_index].name)
  return {str(i):sorted(v) for i,v in sorted(d.items())}
 finally:bpy.data.meshes.remove(m)
r=addon.execute_request(req);assert r['ok'],r
original=values();prep=addon.execute_request({**req,'operation':'prepare_instances'});assert prep['ok'],prep
r=addon.execute_request({**req,'operation':'parameters','instance_collection':prep['prepared_instances']['prepared_collection']});assert r['ok'],r
prepared=values()
report={'ok':original==prepared,'original':original,'prepared':prepared,'mapping':prep['prepared_instances']['objects']}
(out/'result.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
assert report['ok'],report
print('PROTOTYPE_ORDER_PASS')
