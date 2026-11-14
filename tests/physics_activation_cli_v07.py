"""Public activation sweep with bake/cold validation and successful replay skip."""
from pathlib import Path
import os,json,subprocess,sys,hashlib
ROOT=Path(__file__).resolve().parents[1]
out=Path(sys.argv[1]).resolve();source=Path(sys.argv[2]).resolve();out.mkdir(parents=True,exist_ok=False)
os.environ.update(BLENDER_PATH=os.environ.get('BLENDER_PATH',str(ROOT/'runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe')),
 PHYSICS_WORKFLOW_ADDON_DIR=str(ROOT/'tools/physics_nodes_workflow_addon'),BLENDER_USER_CONFIG=str(out/'config'))
source_file=source/'input.blend';before=hashlib.sha256(source_file.read_bytes()).hexdigest()
base={'input_blend':str(source_file),'target_object':'Delayed','bake':True,'verify_cold_reopen':True,
 'parameters':{'frame_start':5,'frame_end':16,'scatter_count':0,'stiffness':0,'ground_z':-100,'wind_turbulence':0}}
request={'base_request':base,'variants':[{'label':'release_8','parameters':{'activation_frame':8}},
                                      {'label':'release_12','parameters':{'activation_frame':12}}]}
p=out/'request.json';p.write_text(json.dumps(request),encoding='utf-8')
def call(label,args):
 process=subprocess.run([sys.executable,str(ROOT/'tools/physics_workflow_cli_v1/cli.py'),*map(str,args)],capture_output=True,text=True,encoding='utf-8',timeout=180)
 (out/(label+'.json')).write_text(process.stdout,encoding='utf-8');(out/(label+'.log')).write_text(process.stderr,encoding='utf-8')
 data=json.loads(process.stdout);assert process.returncode==0 and data['ok'],data
 return data
run=call('sweep',['sweep','--request',p,'--jobs-dir',out/'j','--timeout-seconds','120'])
manifest=Path(run['data']['resume_plan']);plan=json.loads(manifest.read_text(encoding='utf-8'))
assert [c['status'] for c in plan['candidates']]==['success','success']
requests=[c['request'] for c in plan['candidates']]
assert [r['parameters']['activation_frame'] for r in requests]==[8,12]
assert len({r['output_blend'] for r in requests})==2 and len({r['bake_directory'] for r in requests})==2
resume=call('resume',['resume','--manifest',manifest,'--timeout-seconds','120'])
assert resume['data']['skipped']==2 and resume['data']['rerun']==0,resume
new_plan=json.loads(manifest.read_text(encoding='utf-8'))
assert [len(c['attempts']) for c in new_plan['candidates']]==[1,1]
assert hashlib.sha256(source_file.read_bytes()).hexdigest()==before
(out/'result.json').write_text(json.dumps({'ok':True,'manifest':str(manifest),'requests':requests,'source_sha256':before,
 'candidate_attempts':[len(c['attempts']) for c in new_plan['candidates']]},indent=2),encoding='utf-8')
print('ACTIVATION_CLI_PASS')
