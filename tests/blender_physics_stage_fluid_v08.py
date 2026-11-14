"""Full-frame lightweight particle splash and actual-population evidence."""
import bpy,json,sys,time,math
from pathlib import Path
from mathutils import Vector
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from physics_nodes_workflow_addon import execute_request,register,geometry_evidence,contract,bake,subgroups
mode,folder,scale=sys.argv[sys.argv.index('--')+1:];out=Path(folder).resolve();large=scale=='large'
def mesh(name,verts,faces):
 m=bpy.data.meshes.new(name);m.from_pydata(verts,[],faces);m.update();o=bpy.data.objects.new(name,m);bpy.context.scene.collection.objects.link(o);return o
def material(name,color):
 m=bpy.data.materials.new(name);m.diffuse_color=(*color,1);m.use_nodes=True
 p=m.node_tree.nodes.get('Principled BSDF');p.inputs['Base Color'].default_value=(*color,1);p.inputs['Roughness'].default_value=.25
 return m
def observe(obj):
 geo=obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry();m=geo.mesh
 # Empty reconstruction does not prove that the simulation population is zero.
 if not m or not len(m.vertices):return {'particles':None,'vertices':0,'bounds':None,'finite':True}
 attr=m.attributes.get('pnw_particle_count');assert attr and attr.data_type=='INT'
 count=attr.data[0].value;assert all(x.value==count for x in attr.data)
 coords=[tuple(v.co) for v in m.vertices]
 return {'particles':int(count),'vertices':len(coords),'bounds':[[min(p[k] for p in coords) for k in range(3)],[max(p[k] for p in coords) for k in range(3)]], 'finite':all(math.isfinite(x) for p in coords for x in p)}
if mode=='build':
 out.mkdir(parents=True,exist_ok=False);bpy.ops.wm.read_factory_settings(use_empty=True);register()
 scene=bpy.context.scene;end=250 if large else 12;scene.frame_start=1;scene.frame_end=end;scene.render.fps=24
 obj=mesh('ParticleSplash',[(-.2,-.2,1.6),(.2,-.2,1.6),(.2,.2,1.6),(-.2,.2,1.6)],[(0,1,2,3)])
 obj.data.materials.append(material('SplashBlue',(.025,.32,.65)))
 # A slanted finite obstacle intercepts part of the spray; floor remains explicit.
 ramp=mesh('Ramp',[(.5,-.9,.2),(1.4,-.9,.7),(1.4,.9,.7),(.5,.9,.2)],[(0,1,2,3)])
 ramp.data.materials.append(material('RampWarm',(.5,.18,.06)))
 floor=mesh('DisplayFloor',[(-8,-8,0),(8,-8,0),(8,8,0),(-8,8,0)],[(0,1,2,3)])
 floor.data.materials.append(material('Floor',(.1,.13,.18)))
 request={'target_object':obj.name,'preset_id':'fluid_fountain_sdf','collider_object':ramp.name,'bake':True,
          'bake_directory':str(out/'cache'),'parameters':{'frame_start':1,'frame_end':end,'substeps':4,'seed':42,
          'emit_rate':520 if large else 5,'emit_end_frame':101 if large else 8,'lifetime':500,
          'particle_radius':.06,'contact_thickness':.06,'sdf_voxel_size':.06,
          'emit_speed':2,'wind_vector':[.05,0,0],'wind_turbulence':0,'ground_z':0,'drag':.08,'restitution':.35,'friction':.3}}
 bpy.ops.wm.save_as_mainfile(filepath=str(out/'source.blend'));start=time.perf_counter()
 result=execute_request(request);elapsed=time.perf_counter()-start
 (out/'response.json').write_text(json.dumps(result,indent=2),encoding='utf-8');assert result['ok'] and result['bake']['baked'],result
 rows={};samples={};frames=[1,30,60,120,end] if large else [1,6,end]
 for f in range(1,end+1):
  scene.frame_set(f);rows[str(f)]=observe(obj);assert rows[str(f)]['finite']
  if f in frames:samples[str(f)]=geometry_evidence.sample_frame(obj,f)
 assert rows[str(end)]['particles']>=(50000 if large else 20),rows[str(end)]
 assert rows[str(end)]['particles']==rows[str(102 if large else 9)]['particles']
 scene.render.engine='CYCLES';scene.cycles.samples=24;scene.render.resolution_x=960;scene.render.resolution_y=640;scene.render.resolution_percentage=100
 if scene.world is None:scene.world=bpy.data.worlds.new('ReviewWorld')
 scene.world.color=(.13,.13,.13)
 camdata=bpy.data.cameras.new('ReviewCamera');cam=bpy.data.objects.new('ReviewCamera',camdata);scene.collection.objects.link(cam);scene.camera=cam
 cam.location=(7,-9,6);cam.rotation_euler=(Vector((0,0,1))-cam.location).to_track_quat('-Z','Y').to_euler();camdata.type='ORTHO';camdata.ortho_scale=9
 for name,loc,energy,size in [('Key',(1,-4,7),1200,5),('Fill',(-4,2,5),900,4)]:
  data=bpy.data.lights.new(name,'AREA');light=bpy.data.objects.new(name,data);scene.collection.objects.link(light);light.location=loc;light.rotation_euler=(-light.location).to_track_quat('-Z','Y').to_euler();data.energy=energy;data.shape='DISK';data.size=size
 bpy.ops.wm.save_as_mainfile(filepath=str(out/'output.blend'))
 (out/'build.json').write_text(json.dumps({'ok':True,'request':request,'blender':bpy.app.version_string,'bake_seconds':elapsed,'frames':rows,'samples':samples,'scope':'lightweight particle splash to SDF mesh, not pressure liquid'},indent=2),encoding='utf-8')
elif mode in ['cold','render','render2','playback']:
 expected=json.loads((out/'build.json').read_text(encoding='utf-8'));bpy.ops.wm.open_mainfile(filepath=str(out/'output.blend'));obj=bpy.data.objects['ParticleSplash']
 mod=obj.modifiers['PhysicsNodes'];core=mod.node_group.nodes['PNW_CoreNode'].node_tree
 assert bake.is_cache_valid(obj,mod,contract.normalize_request(expected['request'])['parameters_hash'],subgroups.tree_fingerprint(core))[0]
 if mode=='cold':
  rows=[]
  for f in list(reversed([int(x) for x in expected['samples']]))+[1,expected['request']['parameters']['frame_end']]:
   bpy.context.scene.frame_set(f);actual=geometry_evidence.sample_frame(obj,f);assert actual['state_sha256']==expected['samples'][str(f)]['state_sha256'],f
   assert observe(obj)==expected['frames'][str(f)],f;rows.append({'frame':f,'passed':True})
  (out/'cold.json').write_text(json.dumps({'ok':True,'checks':rows},indent=2),encoding='utf-8')
 elif mode in ['render','render2']:
  if mode=='render2':
   cam=bpy.context.scene.camera;cam.location=(-7,-6,3.5);cam.rotation_euler=(Vector((0,0,.8))-cam.location).to_track_quat('-Z','Y').to_euler()
  for f in ([30,100,250] if large else [6,12]):
   prefix='preview2' if mode=='render2' else 'preview'
   bpy.context.scene.frame_set(f);bpy.context.scene.render.filepath=str(out/f'{prefix}-{f:03}.png');bpy.ops.render.render(write_still=True)
 else:
  scene=bpy.context.scene;scene.render.engine='BLENDER_WORKBENCH';scene.render.resolution_percentage=67
  scene.display.shading.light='STUDIO';scene.display.shading.color_type='MATERIAL';scene.display.shading.show_shadows=True
  scene.display.shading.show_cavity=True;scene.display.shading.cavity_type='BOTH'
  frames_dir=out/'playback-frames';frames_dir.mkdir(exist_ok=False);rows=[];start=time.perf_counter()
  for f in range(scene.frame_start,scene.frame_end+1):
   scene.frame_set(f);actual=observe(obj);assert actual==expected['frames'][str(f)],f
   scene.render.filepath=str(frames_dir/f'{f:04}.png');bpy.ops.render.render(write_still=True);rows.append(f)
  (out/'playback.json').write_text(json.dumps({'ok':True,'frames':rows,'elapsed_seconds':time.perf_counter()-start,'scope':'independent cold sequential evaluation and every-frame rendered playback; not realtime viewport FPS'},indent=2),encoding='utf-8')
else:raise ValueError(mode)
print('STAGE_FLUID_'+mode.upper()+'_PASS')
