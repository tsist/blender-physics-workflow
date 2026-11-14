"""Real CLI prepare -> cold build/bake/export -> independent cache/dependency checks."""
import json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
out=Path(sys.argv[1]).resolve();source=Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False)
blender=Path(os.environ.get('BLENDER_PATH',ROOT/'runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe'))
os.environ.update(BLENDER_PATH=str(blender),PHYSICS_WORKFLOW_ADDON_DIR=str(ROOT/'tools/physics_nodes_workflow_addon'),BLENDER_USER_CONFIG=str(out/'config'))
def call(label,request):
 p=out/(label+'-request.json');p.write_text(json.dumps(request),encoding='utf-8')
 command=[sys.executable,str(ROOT/'tools/physics_workflow_cli_v1/cli.py'),'run','--request',str(p),'--jobs-dir',str(out/'j'),'--timeout-seconds','120']
 process=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=180)
 (out/(label+'-response.json')).write_text(process.stdout,encoding='utf-8');(out/(label+'.log')).write_text(process.stderr,encoding='utf-8')
 data=json.loads(process.stdout);assert process.returncode==0 and data['ok'],data
 return data
base={'input_blend':str(source/'prepared-only.blend'),'output_blend':str(out/'prepared.blend'),
      'target_object':'Target','preset_id':'rigid_drop_pack','operation':'prepare_instances',
      'instance_collection':'OriginalPrototypes','parameters':{'frame_start':1,'frame_end':3,'scatter_count':0}}
first=call('prepare',base);prepared=first['data']['worker']['prepared_instances']['prepared_collection']
build={**base,'input_blend':str(out/'prepared.blend'),'output_blend':str(out/'baked.blend'),
       'operation':'build','instance_collection':prepared,'bake':True,'verify_cold_reopen':True,
       'bake_directory':str(out/'b'),'export_format':'ALEMBIC','export_path':str(out/'prepared.abc')}
second=call('build',build)
assert second['data']['cold_reopen_verify']['passed']
result={'ok':True,'prepared_collection':prepared,'prepare':first['data']['resume_plan'],
        'build':second['data']['resume_plan'],'baked_blend':str(out/'baked.blend'),
        'abc':str(out/'prepared.abc'),'request':build}
(out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
