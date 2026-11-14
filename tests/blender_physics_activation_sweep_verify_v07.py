"""Independently observe activation in each public CLI sweep output."""
import bpy,json,sys
from pathlib import Path
folder=Path(sys.argv[sys.argv.index('--')+1]);report=json.loads((folder/'result.json').read_text(encoding='utf-8'))
rows={}
for request in report['requests']:
 bpy.ops.wm.open_mainfile(filepath=request['output_blend']);obj=bpy.data.objects[request['target_object']]
 activation=request['parameters']['activation_frame'];observed={}
 for frame in [16,5,7,8,10,11,12,16]:
  bpy.context.scene.frame_set(frame);bpy.context.view_layer.update()
  geometry=obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry();p=geometry.instances_pointcloud()
  active=[bool(i.value) for i in p.attributes['pnw_active'].data]
  velocity=[list(i.vector) for i in p.attributes['pnw_velocity'].data]
  assert len(active)==2 and active==[frame>=activation]*2,(activation,frame,active)
  if frame<activation:assert velocity==[[0,0,0]]*2,(activation,frame,velocity)
  else:assert all(v[2]<0 for v in velocity),(activation,frame,velocity)
  observed[str(frame)]={'active':active,'velocity':velocity}
 rows[str(activation)]=observed
assert rows['8']['10']['active']!=rows['12']['10']['active']
(folder/'cold-observed.json').write_text(json.dumps({'ok':True,'candidates':rows},indent=2),encoding='utf-8')
print('ACTIVATION_SWEEP_OBSERVED_PASS')
