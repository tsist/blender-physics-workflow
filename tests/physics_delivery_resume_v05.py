"""Public CLI export -> skip -> damaged dependency -> retry with evidence retained."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from physics_workflow_cli_v1 import resume
from physics_workflow_cli_v1 import runner, job_control
from unittest.mock import patch
from physics_nodes_workflow_addon import export_delivery as delivery

out=Path(sys.argv[1]).resolve(); source=Path(sys.argv[2]).resolve()
out.mkdir(parents=True,exist_ok=False)
cli=ROOT/'tools/physics_workflow_cli_v1/cli.py'
blender=Path(os.environ.get('BLENDER_PATH',ROOT/'runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe'))
os.environ['BLENDER_PATH']=str(blender)
os.environ['PHYSICS_WORKFLOW_ADDON_DIR']=str(ROOT/'tools/physics_nodes_workflow_addon')
os.environ['BLENDER_USER_CONFIG']=str(out/'config')
request={'input_blend':str(source/'source/input.blend'),'output_blend':str(out/'output.blend'),
         'target_object':'DeliveryTarget','preset_id':'rigid_drop_pack','operation':'export',
         'export_format':'STATIC_MESH','export_path':str(out/'static.blend'),'export_frame':2,
         'parameters':{'frame_start':1,'frame_end':3},'verify_cold_reopen':False}
request_path=out/'request.json';request_path.write_text(json.dumps(request),encoding='utf-8')
def call(label,*args,success=True):
    p=subprocess.run([sys.executable,str(cli),*args],capture_output=True,text=True,timeout=180,encoding='utf-8')
    (out/(label+'.stdout.json')).write_text(p.stdout,encoding='utf-8')
    (out/(label+'.stderr.log')).write_text(p.stderr,encoding='utf-8')
    payload=json.loads(p.stdout)
    assert payload['ok']==success and (p.returncode==0)==success,(p.returncode,payload)
    return payload
initial=call('initial','run','--request',str(request_path),'--jobs-dir',str(out/'j'),'--timeout-seconds','120')
manifest=Path(initial['data']['resume_plan'])
original=Path(request['export_path']); original_sha=delivery.sha256_file(original)
sidecar=delivery.manifest_path(original); sidecar_sha=delivery.sha256_file(sidecar)
texture=source/'source/texture.png'; texture_bytes=texture.read_bytes()
plan=resume.read_plan(manifest); candidate=plan['candidates'][0]; attempt=candidate['attempts'][-1]
assert resume.verify_success_attempt(candidate)['ok']
old=copy.deepcopy(candidate);old['attempts'][-1].pop('delivery_manifest')
assert any(r['code']=='delivery_manifest_missing' for r in resume.verify_success_attempt(old)['reasons'])
skip=call('skip','resume','--manifest',str(manifest),'--timeout-seconds','120')
assert skip['data']['skipped']==1
try:
    altered=bytearray(texture_bytes);altered[-1]^=1;texture.write_bytes(altered)
    damaged=resume.verify_success_attempt(candidate)
    assert not damaged['ok'] and any(r['code']=='delivery_manifest_invalid' for r in damaged['reasons'])
finally:
    texture.write_bytes(texture_bytes)
retained=texture.with_name('texture-retained.png'); texture.rename(retained)
try:
    missing=call('missing','resume','--manifest',str(manifest),'--timeout-seconds','120',success=False)
finally:
    retained.rename(texture)
recovered=call('recovered','resume','--manifest',str(manifest),'--timeout-seconds','120')
assert recovered['data']['rerun']==1
assert delivery.sha256_file(original)==original_sha and delivery.sha256_file(sidecar)==sidecar_sha
final=call('final','resume','--manifest',str(manifest),'--timeout-seconds','120')
assert final['data']['skipped']==1
# Structured corruption cases use a separate owned fixture; every check is read-only.
fixture=out/'negative';fixture.mkdir()
primary=fixture/'primary.blend';primary.write_bytes(b'primary')
dep=fixture/'image.png';dep.write_bytes(b'dependency')
ref=delivery.write_manifest(primary,'STATIC_MESH',[delivery.file_record(dep,'material_image')],{'scope':'test'})
mp=Path(ref['path']);valid=json.loads(mp.read_text(encoding='utf-8'));checks={}
mutations={
 'schema':lambda x:x.update(schema='old'),
 'format':lambda x:x.update(export_format='ALEMBIC'),
 'primary_path':lambda x:x['primary'].update(path=str(dep)),
 'file_size_bool':lambda x:x['files'][0].update(size=True),
 'dependency_hash':lambda x:x['files'][0].update(sha256='0'*64),
 'duplicate':lambda x:x['files'].append(copy.deepcopy(x['files'][0])),
 'self_reference':lambda x:x['files'].append(delivery.file_record(mp,'self')),
 'files_type':lambda x:x.update(files={}),
 'nonfinite':lambda x:x['preservation'].update(value=float('nan')),
}
for name,mutate in mutations.items():
    value=copy.deepcopy(valid);mutate(value);mp.write_text(json.dumps(value),encoding='utf-8')
    changed=dict(ref,sha256=delivery.sha256_file(mp))
    try:delivery.verify_manifest(changed,primary,'STATIC_MESH')
    except (OSError,ValueError):checks[name]=True
    else:raise AssertionError(name+' accepted')
mp.write_text(json.dumps(valid),encoding='utf-8')
newref=dict(ref,sha256=delivery.sha256_file(mp))
delivery.verify_manifest(newref,primary,'STATIC_MESH')
mp.write_text('['*1200+'0'+']'*1200,encoding='utf-8')
try:delivery.verify_manifest(dict(ref,sha256=delivery.sha256_file(mp)),primary,'STATIC_MESH')
except ValueError:checks['nested_json']=True
else:raise AssertionError('nested JSON accepted')
assert delivery.verify_export_result({'exported':True,'export_format':'STATIC_MESH'},'STATIC_MESH',None) is None
try:delivery.verify_export_result({'exported':True,'export_format':'STATIC_MESH','saved_path':str(primary)},'STATIC_MESH',None)
except ValueError:checks['disk_export_cannot_use_scene_exemption']=True
else:raise AssertionError('disk exemption accepted')
# Change a real dependency after worker acceptance, exactly at final signing.
late_request=dict(request,output_blend=str(out/'late-output.blend'),export_path=str(out/'late-static.blend'))
sign=resume._record_success_evidence
def change_before_sign(*args):
    altered=bytearray(texture_bytes);altered[-1]^=1;texture.write_bytes(altered)
    return sign(*args)
try:
    with patch.object(resume,'_record_success_evidence',side_effect=change_before_sign):
        late=runner.run_single(late_request,out/'late-j',timeout_seconds=120,memory_limit_mb=2048)
    assert not late.ok and late.error['code']=='EXPORT_DELIVERY_INVALID',late
    late_plan=resume.read_plan(Path(late.data['resume_plan']))
    assert late_plan['candidates'][0]['status']=='failed'
    status=job_control.status_manifest(Path(late.data['resume_plan'])).to_dict()
    (out/'late-status.json').write_text(json.dumps(status,indent=2),encoding='utf-8')
    checks['late_dependency_change_fails_run']=True
finally:
    texture.write_bytes(texture_bytes)
result={'ok':True,'manifest':str(manifest),'old_manifest_rejected':True,'same_size_damage_rejected':True,
        'missing_dependency_resume_failed':True,'recovered_rerun':1,'final_skip':1,'old_delivery_preserved':True,'negative_cases':checks}
(out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
