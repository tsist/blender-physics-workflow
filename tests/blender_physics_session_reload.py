"""Cached template RNA references must not survive a file reset as live objects."""
from pathlib import Path
import bpy,sys,json
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import physics_nodes_workflow_addon as addon
out=Path(sys.argv[sys.argv.index('--')+1]);out.mkdir(parents=True,exist_ok=False)
results=[]
for i in range(2):
    if i: bpy.ops.wm.read_factory_settings(use_empty=False)
    obj=bpy.context.active_object
    r=addon.execute_request({'target_object':obj.name,'preset_id':'rigid_drop_pack','parameters':{'frame_end':2,'scatter_count':0}})
    assert r['ok'],r
    results.append(r['build']['wrapper_name'] if 'build' in r else 'ok')
(out/'result.json').write_text(json.dumps({'pass':True,'builds':results}),encoding='utf-8')
print('SESSION_RELOAD_PASS')
