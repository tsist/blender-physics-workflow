"""Independent Alembic reimport of prepared prototypes, per-face geometry/UV/material."""
import bpy, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from physics_nodes_workflow_addon import export
mode, folder=sys.argv[sys.argv.index('--')+1:];folder=Path(folder)
info=json.loads((folder/'result.json').read_text(encoding='utf-8'))

def snapshot(obj):
 mesh=export._realized_mesh(obj)
 try:
  rows=[]
  for face in mesh.polygons:
   material=mesh.materials[face.material_index]
   corners=[]
   for index in face.loop_indices:
    world=obj.matrix_world @ mesh.vertices[mesh.loops[index].vertex_index].co
    corners.append({'co':[round(float(v),5) for v in world],
                    'uv':{u.name:[round(float(v),5) for v in u.data[index].uv] for u in mesh.uv_layers}})
   rows.append({'material':material.name if material else None,'corners':sorted(corners,key=str)})
  return sorted(rows,key=str)
 finally:bpy.data.meshes.remove(mesh)

if mode=='expected':
 bpy.ops.wm.open_mainfile(filepath=info['baked_blend']);obj=bpy.data.objects['Target']
else:
 bpy.ops.wm.read_factory_settings(use_empty=True)
 assert bpy.ops.wm.alembic_import(filepath=info['abc'],as_background_job=False)=={'FINISHED'}
 meshes=[o for o in bpy.context.scene.objects if o.type=='MESH'];assert len(meshes)==1
 obj=meshes[0]
rows={}
for frame in (3,1,2):
 bpy.context.scene.frame_set(frame);bpy.context.view_layer.update();rows[str(frame)]=snapshot(obj)
 assert {r['material'] for r in rows[str(frame)]}=={'Blue','Green'}
 assert len(rows[str(frame)])==36
path=folder/('abc-expected.json' if mode=='expected' else 'abc-cold.json')
path.write_text(json.dumps({'ok':True,'frames':rows},indent=2),encoding='utf-8')
if mode!='expected':assert rows==json.loads((folder/'abc-expected.json').read_text(encoding='utf-8'))['frames']
print('PREPARED_ABC_'+mode.upper()+'_PASS')
