"""Integrated public-input tests: prototypes and world collider collections."""
import bpy, sys, json, math
from pathlib import Path
from mathutils import Vector
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import contract, core, export
OUT=Path(sys.argv[sys.argv.index('--')+1]);OUT.mkdir(parents=True,exist_ok=False)
results={}
def make(name,pts,faces=(),collection=None):
    mesh=bpy.data.meshes.new(name);mesh.from_pydata(pts,[],faces);mesh.update()
    o=bpy.data.objects.new(name,mesh);(collection or bpy.context.scene.collection).objects.link(o);return o
def snap(o):
    return {'coords':[list(v.co) for v in o.data.vertices],'location':list(o.location),'rotation':list(o.rotation_euler),'scale':list(o.scale),'materials':[m.name if m else None for m in o.data.materials]}
bpy.ops.wm.read_factory_settings(use_empty=True)
coll=bpy.data.collections.new('Prototypes')
faces=[(0,1,3,2),(4,6,7,5),(0,4,5,1),(2,3,7,6),(0,2,6,4),(1,5,7,3)]
protos=[]
for i,size in enumerate([1.,4.]):
    pts=[(x*size+3*i,y*size*.5,z*size*.3) for z in [-1,1] for y in [-1,1] for x in [-1,1]]
    p=make('Prototype'+str(i),pts,faces,coll);p.scale=(2,.7,1.3);p.rotation_euler=(.2,.4,.5);p.location=(20+i,10,3)
    mat=bpy.data.materials.new('Material'+str(i));p.data.materials.append(mat);protos.append(p)
bpy.context.view_layer.update();before=[snap(p) for p in protos]
origins=[(i*10.,0,2) for i in range(4)]
obj=make('scatter',origins)
req={'target_object':obj.name,'preset_id':'rigid_drop_pack','instance_collection':coll.name,'parameters':{'frame_end':2,'scatter_count':0,'particle_radius':.4,'instance_normalize':True}}
def max_radius():
    bpy.context.scene.frame_set(1);m=export._realized_mesh(obj)
    try:return max(min((v.co-Vector(c)).length for c in origins) for v in m.vertices),len(m.vertices),{p.material_index for p in m.polygons}
    finally:bpy.data.meshes.remove(m)
r=addon.execute_request(req);assert r['ok'],r
normalized=max_radius();assert normalized[0]<=.40001 and normalized[1]==32,normalized
old_group=obj.modifiers['PhysicsNodes'].node_group
req['operation']='parameters';req['parameters']['instance_normalize']=False
r=addon.execute_request(req);assert r['ok'],r
legacy=max_radius();assert legacy[0]>1.,legacy
assert obj.modifiers['PhysicsNodes'].node_group==old_group
assert before==[snap(p) for p in protos]
results['instances']={'normalized_max_radius':normalized[0],'legacy_max_radius':legacy[0],'materials':len(normalized[2]),'source_preserved':True,'parameter_update_reused_graph':True}

# Cloth domain uses the complete public core; no hand-injected solver links.
bpy.ops.wm.read_factory_settings(use_empty=True)
coll=bpy.data.collections.new('Colliders');bpy.context.scene.collection.children.link(coll)
for i,h in enumerate([.6,.2]):
    x=-3 if i==0 else 3
    p=make('Plane'+str(i),[(-2,-2,0),(2,-2,0),(2,2,0),(-2,2,0)],[(0,1,2,3)],coll);p.location=(x,0,h)
pts=[];faces=[]
for x in [-3,3]:
    j=len(pts);pts.extend([(x-.3,-.3,1.2),(x+.3,-.3,1.2),(x+.3,.3,1.2),(x-.3,.3,1.2)]);faces.append(tuple(range(j,j+4)))
obj=make('cloth',pts,faces);obj.data.attributes.new('pnw_pinned','FLOAT','POINT')
req=contract.normalize_request({'target_object':obj.name,'preset_id':'cloth_banner_wind','collider_collection':coll.name,'parameters':{'frame_end':40,'substeps':4,'constraint_iterations':4,'restitution':0,'wind_vector':[0,0,0],'wind_turbulence':0,'ground_z':-100,'contact_thickness':.04}})
core.build_or_update_workflow(obj,req)
for f in range(1,41):
    bpy.context.scene.frame_set(f);obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
ev=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());m=ev.to_mesh()
heights=[sum(v.co.z for v in m.vertices if (v.co.x<0)==side)/4 for side in [True,False]]
speed=max(Vector(a.vector).length for a in m.attributes['pnw_velocity'].data)
ev.to_mesh_clear()
assert abs(heights[0]-.64)<1e-4 and abs(heights[1]-.24)<1e-4,heights
assert speed<.01,speed
results['cloth_collection']={'heights':heights,'max_speed':speed,'frame':40}
results['pass']=True
(OUT/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps(results))
