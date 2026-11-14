"""Public build/bake and fresh-process replay of native cloth effectors."""
import bpy, sys, json, math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import contract, bake, subgroups, geometry_evidence
mode, folder=sys.argv[sys.argv.index('--')+1:]
OUT=Path(folder).resolve()

def valid(obj, req):
    mod=obj.modifiers['PhysicsNodes']
    core=mod.node_group.nodes['PNW_CoreNode'].node_tree
    ok, reason, _=bake.is_cache_valid(obj,mod,req['parameters_hash'],subgroups.tree_fingerprint(core))
    assert ok, reason

if mode=='build':
    OUT.mkdir(parents=True,exist_ok=False)
    scene=bpy.context.scene; scene.render.fps=24
    records={}
    for name, options in [('plain',{}),('bend',{'bending':True,'bend_softness':0}),('tear',{'tearing':True,'tear_threshold':1.001})]:
        n=5
        coords=[(x*.5,y*.5,3) for y in range(n) for x in range(n)]
        faces=[]
        for y in range(n-1):
            for x in range(n-1):
                a=y*n+x; faces.extend([(a,a+1,a+n+1),(a,a+n+1,a+n)])
        mesh=bpy.data.meshes.new(name); mesh.from_pydata(coords,[],faces); mesh.update()
        pins=mesh.attributes.new('pnw_pinned','FLOAT','POINT')
        for i,a in enumerate(pins.data): a.value=1. if i%n==0 else 0.
        obj=bpy.data.objects.new(name,mesh); scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active=obj; obj.select_set(True)
        request={'target_object':name,'preset_id':'cloth_banner_wind','bake':True,
                 'bake_directory':str(OUT/name),'parameters':{
                     'frame_end':24,'substeps':2,'constraint_iterations':3,
                     'wind_vector':[15,0,0],'wind_turbulence':0,'ground_z':-100,
                     'stiffness':.5,**options}}
        bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'work.blend'))
        result=addon.execute_request(request); assert result['ok'],result
        req=contract.normalize_request(request); valid(obj,req)
        samples=geometry_evidence.sample_frames(obj,[1,12,24])
        scene.frame_set(24)
        ev=obj.evaluated_get(bpy.context.evaluated_depsgraph_get()); evaluated=ev.to_mesh()
        points=[list(v.co) for v in evaluated.vertices]
        assert all(math.isfinite(c) for p in points for c in p)
        pin_attr=evaluated.attributes.get('pnw_pinned')
        rest=evaluated.attributes.get('pnw_rest_pos')
        pin_error=max(((v.co-rest.data[i].vector).length for i,v in enumerate(evaluated.vertices) if pin_attr.data[i].value>.5),default=0)
        assert pin_error<1e-5,pin_error
        ev.to_mesh_clear()
        records[name]={'request':request,'samples':samples,'pin_error':pin_error,'f24_points':points}
    assert len(records['tear']['f24_points'])>25,records['tear']
    assert len(records['plain']['f24_points'])==len(records['bend']['f24_points'])==25
    delta=max(math.dist(a,b) for a,b in zip(records['plain']['f24_points'],records['bend']['f24_points']))
    assert delta>1e-3,delta
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'output.blend'))
    (OUT/'build.json').write_text(json.dumps({'pass':True,'bend_response_delta':delta,'records':records},indent=2),encoding='utf-8')
    print('DEFORMATION_BUILD_PASS',delta,len(records['tear']['f24_points']))
elif mode=='cold':
    expected=json.loads((OUT/'build.json').read_text(encoding='utf-8'))
    bpy.ops.wm.open_mainfile(filepath=str(OUT/'output.blend'),use_scripts=False)
    matches={}
    for name, record in expected['records'].items():
        obj=bpy.data.objects[name]; req=contract.normalize_request(record['request']); valid(obj,req)
        matches[name]=[geometry_evidence.sample_frame(obj,f)['geometry_sha256']==record['samples']['frames'][str(f)]['geometry_sha256'] for f in [24,1,12,24]]
        assert all(matches[name]),matches
    (OUT/'cold.json').write_text(json.dumps({'pass':True,'matches':matches},indent=2),encoding='utf-8')
    print('DEFORMATION_COLD_PASS')
else: raise ValueError(mode)
