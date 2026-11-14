"""Real Blender production/cold-read evidence for material/UV/image delivery."""
import json
import sys
from pathlib import Path
import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from physics_nodes_workflow_addon import export, export_delivery, register

args = sys.argv[sys.argv.index('--')+1:]
mode, directory = args[0], Path(args[1]).resolve()
directory.mkdir(parents=True, exist_ok=True)

def dump(name, data):
    (directory/name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')

def snapshot(obj):
    dg=bpy.context.evaluated_depsgraph_get()
    ev=obj.evaluated_get(dg)
    mesh=bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=dg)
    try:
        rows=[]
        for poly in mesh.polygons:
            mat=ev.material_slots[poly.material_index].material
            row={'material':mat.name if mat else None, 'corners':[]}
            for li in poly.loop_indices:
                co=ev.matrix_world @ mesh.vertices[mesh.loops[li].vertex_index].co
                row['corners'].append({'co':[round(float(v),5) for v in co],
                   'uv':{u.name:[round(float(v),5) for v in u.data[li].uv] for u in mesh.uv_layers}})
            row['corners'].sort(key=lambda x:x['co'])
            rows.append(row)
        rows.sort(key=lambda r:str(r['corners']))
        return {'faces':rows,'uv_layers':[[u.name,u.active_render] for u in mesh.uv_layers],
                'active_uv':mesh.uv_layers.active.name,'materials':[s.material.name if s.material else None for s in ev.material_slots]}
    finally:
        bpy.data.meshes.remove(mesh)

def request(obj,fmt,path):
    return {'operation':'export','preset_id':'rigid_drop_pack','target_object':obj.name,
      'export_format':fmt,'export_path':str(path),'export_frame':2,
      'parameters':{'frame_start':1,'frame_end':3,'sdf_voxel_size':.2}}

if mode=='produce':
    bpy.ops.wm.read_factory_settings(use_empty=True)
    register()
    bpy.ops.mesh.primitive_cube_add()
    obj=bpy.context.object; obj.name='DeliveryTarget'
    obj.scale=(-1.5,.8,1.2)
    base=bpy.data.materials.new('OriginalData')
    obj.data.materials.append(base); obj.data.materials.append(base)
    for i,slot in enumerate(obj.material_slots):
        slot.link='OBJECT'; slot.material=bpy.data.materials.new('Override'+str(i)); slot.material.use_nodes=True
    for i,p in enumerate(obj.data.polygons): p.material_index=i%2
    obj.data.uv_layers.new(name='SecondUV')
    for i,uv in enumerate(obj.data.uv_layers[1].data): uv.uv=(i*.137, i*.071)
    obj.data.uv_layers.active_index=0; obj.data.uv_layers[1].active_render=True
    # Shared data must not be changed by exporting the target.
    sibling=bpy.data.objects.new('SharedNonTarget',obj.data); bpy.context.scene.collection.objects.link(sibling)
    sibling.location.x=12
    (directory/'source').mkdir()
    img=bpy.data.images.new('ImageFile',width=2,height=2)
    img.pixels=[.2,.4,.8,1]*4
    img.filepath_raw=str(directory/'source/texture.png'); img.file_format='PNG'; img.save()
    bpy.data.images.remove(img)
    img=bpy.data.images.load(str(directory/'source/texture.png')); img.name='ImageFile'
    tex=obj.material_slots[0].material.node_tree.nodes.new('ShaderNodeTexImage'); tex.image=img
    principled=obj.material_slots[0].material.node_tree.nodes.get('Principled BSDF')
    obj.material_slots[0].material.node_tree.links.new(tex.outputs['Color'],principled.inputs['Base Color'])
    for f in (1,3):
        obj.location.x=f; obj.keyframe_insert('location',frame=f)
    bpy.context.scene.frame_set(2,subframe=.25)
    bpy.ops.wm.save_as_mainfile(filepath=str(directory/'source/input.blend'))
    img.filepath='//texture.png'
    bpy.ops.wm.save_as_mainfile(filepath=str(directory/'source/input.blend'))
    original_path=img.filepath; original_file=bpy.data.filepath
    snapshots={}
    for f in (1,2,3): bpy.context.scene.frame_set(f); snapshots[str(f)]=snapshot(obj)
    bpy.context.scene.frame_set(2,subframe=.25)
    original_objects=set(bpy.data.objects.keys()); original_groups=set(bpy.data.node_groups.keys())
    static=export.export_workflow_result(obj,request(obj,'STATIC_MESH',directory/'delivery/nested/static.blend'))
    abc=export.export_workflow_result(obj,request(obj,'ALEMBIC',directory/'delivery/mesh.abc'))
    assert img.filepath==original_path and bpy.data.filepath==original_file
    assert [m.name for m in obj.data.materials]==['OriginalData','OriginalData']
    assert [s.material.name for s in obj.material_slots]==['Override0','Override1']
    assert set(bpy.data.objects.keys())==original_objects and set(bpy.data.node_groups.keys())==original_groups
    assert [bpy.context.scene.frame_current,bpy.context.scene.frame_subframe]==[2,.25]
    for res in (static,abc):
        export_delivery.verify_export_result(res,res['export_format'],res.get('saved_path') or res.get('export_path'))
    # Known missing image must fail before primary or manifest creation.
    img.filepath='//missing.png'
    missing=directory/'delivery/missing.blend'
    try: export.export_workflow_result(obj,request(obj,'STATIC_MESH',missing))
    except ValueError as exc: missing_error=str(exc)
    else: raise AssertionError('missing image accepted')
    assert not missing.exists() and set(bpy.data.objects.keys())==original_objects
    img.filepath=original_path
    # A real UI operator must expose limitations and sign the delivery.
    obj.pnw_settings.export_format='STATIC_MESH'; obj.pnw_settings.export_path=str(directory/'delivery/ui.blend'); obj.pnw_settings.export_frame=2
    assert bpy.ops.pnw.export_result()=={'FINISHED'}
    assert 'External image' in obj.pnw_settings.last_export_summary
    dump('produce.json',{'ok':True,'static':static,'abc':abc,'expected':snapshots,'missing_error':missing_error,'ui_summary':obj.pnw_settings.last_export_summary})
elif mode=='static':
    expected=json.loads((directory/'produce.json').read_text(encoding='utf-8'))
    bpy.ops.wm.open_mainfile(filepath=expected['static']['saved_path'])
    obj=bpy.data.objects[expected['static']['static_object_name']]
    actual=snapshot(obj)
    assert actual==expected['expected']['2'], (actual,expected['expected']['2'])
    img=bpy.data.images['ImageFile']; img.reload()
    path=Path(bpy.path.abspath(img.filepath)).resolve()
    assert path==directory/'source/texture.png' and len(img.pixels)==16
    assert abs(img.pixels[0]-.2)<.01
    links=obj.data.materials[0].node_tree.links
    assert any(l.from_node.type=='TEX_IMAGE' and l.to_socket.name=='Base Color' for l in links)
    dump('static-cold.json',{'ok':True,'actual':actual,'image_path':str(path),'pixels':list(img.pixels)})
elif mode=='abc':
    expected=json.loads((directory/'produce.json').read_text(encoding='utf-8'))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.alembic_import(filepath=expected['abc']['export_path'],as_background_job=False)
    objs=[o for o in bpy.context.scene.objects if o.type=='MESH']; assert len(objs)==1
    rows={}
    for frame in (1,2,3):
        bpy.context.scene.frame_set(frame); rows[str(frame)]=snapshot(objs[0])
    dump('abc-cold-observed.json',rows)
    for frame,actual in rows.items():
        exp=expected['expected'][frame]
        assert actual['faces']==exp['faces'], (frame,actual,exp)
        assert set(actual['materials'])==set(exp['materials'])
    dump('abc-cold.json',{'ok':True,'frames':rows})
