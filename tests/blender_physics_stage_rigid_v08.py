"""Representative 200-body scene and separate 2000-body sparse load, not interchangeable."""
import bpy,json,sys,time,math,hashlib
from pathlib import Path
from mathutils import Vector,kdtree
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from physics_nodes_workflow_addon import execute_request,register,bake,contract,subgroups
mode,folder,scale=sys.argv[sys.argv.index('--')+1:];out=Path(folder).resolve();large=scale=='large'

def mesh(name,verts,faces):
 m=bpy.data.meshes.new(name);m.from_pydata(verts,[],faces);m.update();o=bpy.data.objects.new(name,m);bpy.context.scene.collection.objects.link(o);return o

def material(name,color):
 m=bpy.data.materials.new(name);m.diffuse_color=(*color,1);m.use_nodes=True
 p=m.node_tree.nodes.get('Principled BSDF');p.inputs['Base Color'].default_value=(*color,1);p.inputs['Roughness'].default_value=.32;return m

def snapshot(obj):
 geo=obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry();points=geo.instances_pointcloud();assert points is not None
 return {"positions":[[float(x) for x in (obj.matrix_world @ d.value).translation] for d in points.attributes['instance_transform'].data],
         "velocities":[[float(x) for x in d.vector] for d in points.attributes['pnw_velocity'].data],
         "rotations":[[float(x) for x in d.vector] for d in points.attributes['pnw_rotation'].data],
         "active":[bool(d.value) for d in points.attributes['pnw_active'].data]}

def measure(row,previous):
 points=row['positions'];tree=kdtree.KDTree(len(points))
 for i,p in enumerate(points):tree.insert(p,i)
 tree.balance();overlap=0
 for i,p in enumerate(points):
  near=[d for _,j,d in tree.find_n(p,2) if j!=i];overlap=max(overlap,.4-min(near))
 speeds=[Vector(v).length for v in row['velocities']]
 delta=max((Vector(a)-Vector(b)).length for a,b in zip(points,previous['positions'])) if previous else 0
 return {'count':len(points),'finite':all(math.isfinite(x) for key in ['positions','velocities','rotations'] for p in row[key] for x in p),
  'min_z':min(p[2] for p in points),'max_z':max(p[2] for p in points),'max_overlap':max(0,overlap),'max_speed':max(speeds),'mean_speed':sum(speeds)/len(speeds),'max_frame_delta':delta}

def valid(obj,request):
 mod=obj.modifiers['PhysicsNodes'];core=mod.node_group.nodes['PNW_CoreNode'].node_tree
 return bake.is_cache_valid(obj,mod,contract.normalize_request(request)['parameters_hash'],subgroups.tree_fingerprint(core))[0]

if mode=='build':
 out.mkdir(parents=True,exist_ok=False);bpy.ops.wm.read_factory_settings(use_empty=True);register();scene=bpy.context.scene
 scene.frame_start=1;scene.frame_end=250;scene.render.fps=24
 if large:verts=[((i%50)*.8-19.6,(i//50)*.8-15.6,2+(.15*(i%3))) for i in range(2000)]
 else:verts=[((i%10)*.44-1.98,((i//10)%10)*.44-1.98,2.5+(i//100)*.55) for i in range(200)]
 obj=mesh('RigidBodies',verts,[]);obj.data.materials.append(material('Bodies',(.035,.34,.62)))
 collider=None
 if not large:
  # Normals point into the container; the ramp faces up and toward -X.
  v=[(-2.7,-2.7,0),(-2.7,2.7,0),(-2.7,2.7,4),(-2.7,-2.7,4),
     (2.7,2.7,0),(2.7,-2.7,0),(2.7,-2.7,4),(2.7,2.7,4),
     (2.7,-2.7,0),(-2.7,-2.7,0),(-2.7,-2.7,4),(2.7,-2.7,4),
     (-2.7,2.7,0),(2.7,2.7,0),(2.7,2.7,4),(-2.7,2.7,4),
     (.3,-2.65,.1),(2.65,-2.65,1.2),(2.65,2.65,1.2),(.3,2.65,.1)]
  collider=mesh('ContainerAndRamp',v,[(i,i+1,i+2,i+3) for i in range(0,20,4)])
  collider.data.materials.append(material('Container',(.45,.20,.07)))
 floor=mesh('Floor',[(-24,-20,0),(24,-20,0),(24,20,0),(-24,20,0)],[(0,1,2,3)])
 floor.data.materials.append(material('Floor',(.13,.16,.21)))
 request={'target_object':obj.name,'preset_id':'rigid_drop_pack','bake':True,'bake_directory':str(out/'cache'),
  'parameters':{'frame_start':1,'frame_end':250,'substeps':4,'seed':42,'scatter_count':0,'activation_frame':12,
  'particle_radius':.2,'contact_thickness':.2,'contact_iterations':3,'stiffness':0,'rest_length':0,'gravity':[0,0,-9.81],
  'drag':.12,'restitution':.35,'friction':.45,'sleep_velocity':.08,'wind_turbulence':0,'wind_vector':[0,0,0],'ground_z':0}}
 if collider:request['collider_object']=collider.name
 bpy.ops.wm.save_as_mainfile(filepath=str(out/'source.blend'));start=time.perf_counter();result=execute_request(request);elapsed=time.perf_counter()-start
 (out/'response.json').write_text(json.dumps(result,indent=2));assert result['ok'] and result['bake']['baked'],result
 records={};metrics={};previous=None
 for f in range(1,251):
  scene.frame_set(f);row=snapshot(obj);records[str(f)]=row;metrics[str(f)]=measure(row,previous);previous=row
 scene.render.engine='CYCLES';scene.cycles.samples=24;scene.render.resolution_x=960;scene.render.resolution_y=640;scene.render.resolution_percentage=100
 if scene.world is None:scene.world=bpy.data.worlds.new('World')
 scene.world.color=(.13,.13,.13)
 camdata=bpy.data.cameras.new('ReviewCamera');cam=bpy.data.objects.new('ReviewCamera',camdata);scene.collection.objects.link(cam);scene.camera=cam
 cam.location=(9,-12,12) if not large else (35,-40,40);cam.rotation_euler=(-cam.location).to_track_quat('-Z','Y').to_euler();camdata.type='ORTHO';camdata.ortho_scale=9 if not large else 55
 for name,loc,energy,size in [('Key',(1,-4,10),1500,6),('Fill',(-4,2,8),1000,5)]:
  data=bpy.data.lights.new(name,'AREA');light=bpy.data.objects.new(name,data);scene.collection.objects.link(light);light.location=loc;light.rotation_euler=(-light.location).to_track_quat('-Z','Y').to_euler();data.energy=energy;data.size=size
 bpy.ops.wm.save_as_mainfile(filepath=str(out/'output.blend'))
 checks={'finite_count':all(m['finite'] and m['count']==len(verts) for m in metrics.values()),
  'waiting_preserved':all(records[str(f)]==records['1'] for f in range(1,12)),
  'triggered':all(records['12']['active']) and records['12']['positions']!=records['11']['positions'],
  'ground_clearance':min(m['min_z'] for m in metrics.values())>=.2-1e-4,
  'late_overlap':max(metrics[str(f)]['max_overlap'] for f in range(230,251))<=1e-4,
  'late_rest_delta':max(metrics[str(f)]['max_frame_delta'] for f in range(230,251))<=1e-4}
 (out/'build.json').write_text(json.dumps({'ok':all(checks.values()),'checks':checks,'request':request,'bake_seconds':elapsed,'metrics':metrics,'scope':'2000 sparse load' if large else '200 container ramp trigger resting scene'},indent=2))
 (out/'states.json').write_text(json.dumps(records,separators=(',',':')))
 print(json.dumps({'checks':checks,'bake_seconds':elapsed,'last':metrics['250']}));assert all(checks.values()),checks
elif mode in ['cold','render','render2','playback']:
 expected=json.loads((out/'build.json').read_text());states=json.loads((out/'states.json').read_text());bpy.ops.wm.open_mainfile(filepath=str(out/'output.blend'))
 obj=bpy.data.objects['RigidBodies'];assert valid(obj,expected['request']);scene=bpy.context.scene
 if mode=='cold':
  for f in [250,1,11,12,60,120,250]:scene.frame_set(f);assert snapshot(obj)==states[str(f)],f
  (out/'cold.json').write_text(json.dumps({'ok':True,'frames':[250,1,11,12,60,120,250]}))
 else:
  if mode=='render2':
   cam=scene.camera;cam.location=(1,-1,20) if not large else (-35,-40,40);cam.rotation_euler=(-cam.location).to_track_quat('-Z','Y').to_euler()
  elif not large:
   cam=scene.camera;cam.location=(4,-5,18);cam.rotation_euler=(-cam.location).to_track_quat('-Z','Y').to_euler()
  if mode=='playback':
   scene.render.engine='BLENDER_WORKBENCH';scene.render.resolution_x=640;scene.render.resolution_y=426
   scene.display.shading.color_type='MATERIAL';scene.display.shading.show_shadows=True;scene.display.shading.show_cavity=True
   (out/'playback-frames').mkdir(exist_ok=False)
  for f in (range(1,251) if mode=='playback' else [12,30,250]):
   scene.frame_set(f);assert snapshot(obj)==states[str(f)],f
   scene.render.filepath=str(out/('playback-frames' if mode=='playback' else '.')/f'{mode}-{f:04}.png');bpy.ops.render.render(write_still=True)
  (out/f'{mode}.json').write_text(json.dumps({'ok':True,'frames':250 if mode=='playback' else [12,30,250]}))
else:raise ValueError(mode)
