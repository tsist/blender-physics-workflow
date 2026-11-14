"""Packed-image cold read and preflight image/sidecar refusal boundaries."""
import bpy,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from physics_nodes_workflow_addon import export
args=sys.argv[sys.argv.index('--')+1:];mode=args[0];out=Path(args[1]);source=Path(args[2]) if len(args)>2 else None
if mode=='produce':
 out.mkdir(parents=True,exist_ok=False)
 bpy.ops.wm.open_mainfile(filepath=str(source/'source/input.blend'))
 obj=bpy.data.objects['DeliveryTarget'];img=bpy.data.images['ImageFile'];img.pack()
 original_filepath=img.filepath
 req={'target_object':obj.name,'preset_id':'rigid_drop_pack','export_format':'STATIC_MESH','export_frame':2,'parameters':{'frame_start':1,'frame_end':3}}
 packed=export.export_workflow_result(obj,dict(req,export_path=str(out/'packed.blend')))
 manifest=json.loads(Path(packed['delivery_manifest']['path']).read_text(encoding='utf-8'))
 assert not manifest['files'] and img.packed_file and img.filepath==original_filepath
 results={'packed':packed}
 bpy.data.libraries.write(str(out/'images.blend'),{img})
 with bpy.data.libraries.load(str(out/'images.blend'),link=True) as (data_from,data_to):
  data_to.images=[img.name]
 linked=data_to.images[0];assert linked.name==img.name and linked.library
 nodes=obj.material_slots[0].material.node_tree.nodes
 linked_node=nodes.new('ShaderNodeTexImage');linked_node.image=linked
 try:export.export_workflow_result(obj,dict(req,export_path=str(out/'linked.blend')))
 except ValueError as exc:results['same_name_linked']=str(exc);assert 'linked material dependency' in str(exc)
 else:raise AssertionError('same name linked image accepted')
 nodes.remove(linked_node)
 ies=nodes.new('ShaderNodeTexIES');ies.mode='EXTERNAL';ies.filepath=str(out/'missing.ies')
 try:export.export_workflow_result(obj,dict(req,export_path=str(out/'ies.blend')))
 except ValueError as exc:results['external_ies']=str(exc);assert 'IES' in str(exc)
 else:raise AssertionError('external IES accepted')
 nodes.remove(ies)
 for kind in ('TILED','SEQUENCE','MOVIE'):
  img.source=kind
  path=out/(kind+'.blend')
  try:export.export_workflow_result(obj,dict(req,export_path=str(path)))
  except ValueError as exc:results[kind]=str(exc)
  else:raise AssertionError(kind+' accepted')
  assert not path.exists()
 img.source='FILE'
 img.pixels[0]=.8
 assert img.is_dirty
 try:export.export_workflow_result(obj,dict(req,export_path=str(out/'dirty.blend')))
 except ValueError as exc:results['dirty']=str(exc)
 else:raise AssertionError('dirty packed image accepted')
 path=out/'existing.blend';sidecar=Path(str(path)+'.pnw-export.json');sidecar.write_text('sentinel')
 try:export.export_workflow_result(obj,dict(req,export_path=str(path)))
 except ValueError as exc:results['existing_sidecar']=str(exc)
 else:raise AssertionError('sidecar overwritten')
 assert not path.exists() and sidecar.read_text()=='sentinel'
 (out/'result.json').write_text(json.dumps({'ok':True,**results},indent=2),encoding='utf-8')
else:
 data=json.loads((out/'result.json').read_text(encoding='utf-8'))
 bpy.ops.wm.open_mainfile(filepath=data['packed']['saved_path'])
 img=bpy.data.images['ImageFile'];assert img.packed_file and abs(img.pixels[0]-.2)<.01
 (out/'cold.json').write_text(json.dumps({'ok':True,'packed_bytes':img.packed_file.size,'pixels':list(img.pixels)}))
