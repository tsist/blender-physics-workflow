"""Actual UI-operator authoring and export path regression in isolated Blender."""
from pathlib import Path
import json
import sys
import bpy

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
from physics_nodes_workflow_addon import capabilities
OUT=Path(sys.argv[sys.argv.index('--')+1]).resolve()
OUT.mkdir(parents=True,exist_ok=False)
addon.register()
source=bpy.context.active_object
source.name='ProtectedSource'
s=source.pnw_settings
s.frame_end=3
s.scatter_count=0
s.wind_turbulence=0
s.wind_vector=(0,0,0)
count=len(bpy.data.objects)
assert s.protect_source
assert bpy.ops.pnw.build_workflow()=={'FINISHED'}
working=bpy.context.active_object
assert working!=source and working.data!=source.data and len(source.modifiers)==0
assert working.pnw_settings.frame_end==3
assert len(bpy.data.objects)==count+1
assert bpy.ops.pnw.build_workflow()=={'FINISHED'}
assert bpy.context.active_object==working and len(bpy.data.objects)==count+1
mod=working.modifiers['PhysicsNodes']
old=mod.node_group
user=old.nodes.new('ShaderNodeValue');user.name='AuthoredMarker';user.outputs[0].default_value=77
assert bpy.ops.pnw.update_parameters()=={'FINISHED'}
assert old.nodes['AuthoredMarker'].outputs[0].default_value==77
assert bpy.ops.pnw.rebuild_workflow()=={'FINISHED'}
assert mod.node_group!=old and old.use_fake_user and old.nodes['AuthoredMarker'].outputs[0].default_value==77
ws=working.pnw_settings
ws.export_frame=3
ws.export_path=str(OUT/'static.blend')
assert bpy.ops.pnw.export_result()=={'FINISHED'}
assert (OUT/'static.blend').is_file()
assert len(source.modifiers)==0
ws.export_format='OPENVDB'
ws.export_path=str(OUT/'surface.vdb')
assert bpy.ops.pnw.export_result()=={'FINISHED'}
import openvdb
grids, metadata = openvdb.readAll(str(OUT/'surface.vdb'))
assert len(grids)==1 and grids[0].activeVoxelCount()>0
ws.export_format='ALEMBIC'
# The hidden single-frame field must not constrain an Alembic sequence.
ws.export_frame=1
ws.frame_start=2
ws.frame_end=3
ws.export_path=str(OUT/'sequence.abc')
assert bpy.ops.pnw.update_parameters()=={'FINISHED'}
assert bpy.ops.pnw.export_result()=={'FINISHED'}
assert (OUT/'sequence.abc').stat().st_size>0
assert len(source.modifiers)==0
assessment=capabilities.runtime_capabilities()
assert assessment['qualification']['limitations'][0]['owner']=='plugin_solver'
assert all(not x['blocks_unrelated_workflows'] for x in assessment['qualification']['limitations'])
payload={'pass':True,'source_preserved':True,'working_copy':working.name,'repeat_build_reused':True,
         'old_graph_retained':old.name,'static_export':str(OUT/'static.blend'),
         'vdb_export':str(OUT/'surface.vdb'),'vdb_active_voxels':grids[0].activeVoxelCount(),
         'alembic_export':str(OUT/'sequence.abc'),'assessment':assessment}
(OUT/'results.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
print('AUTHORING_V03_PASS')
addon.unregister()
