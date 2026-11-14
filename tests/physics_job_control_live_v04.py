"""Real CLI batch cancellation/replay using the exact supplied candidate code.

Host Python args: CLI_FILE ADDON_DIR BLENDER_EXE INPUT_BLEND TARGET OUT_DIR.
INPUT_BLEND is an immutable small mesh fixture (e.g. two separated points).
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

cli, addon, blender, source, target, destination = sys.argv[1:]
OUT=Path(destination).resolve()
OUT.mkdir(parents=True,exist_ok=False)
source=Path(source).resolve()
source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
env=dict(os.environ,BLENDER_PATH=blender,PHYSICS_WORKFLOW_ADDON_DIR=addon)
env['PYTHONDONTWRITEBYTECODE']='1'
prefix=[sys.executable,'-B',cli]
flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
events=[]
owned=[]
manifest=None

def invoke(*args):
    result=subprocess.run(prefix+list(args),env=env,capture_output=True,text=True,
                          encoding='utf-8',creationflags=flags,timeout=30)
    data=json.loads(result.stdout)
    return result.returncode,data

def start(label,*args):
    stdout=(OUT/(label+'.stdout.log')).open('w',encoding='utf-8')
    stderr=(OUT/(label+'.stderr.log')).open('w',encoding='utf-8')
    process=subprocess.Popen(prefix+list(args),env=env,stdout=stdout,stderr=stderr,creationflags=flags)
    owned.append((process,stdout,stderr))
    return process

def status():
    code,result=invoke('status','--manifest',str(manifest))
    assert code==0,result
    return result['data']

def wait_phase(process,index,previous_generation=None):
    deadline=time.monotonic()+70
    while time.monotonic()<deadline:
        state=status()
        generation=state.get('executor',{}).get('generation')
        if state.get('phase')=='worker_running' and (state.get('candidate') or {}).get('index')==index:
            if previous_generation is None or generation!=previous_generation:
                events.append({'phase':state['phase'],'generation':generation,'counts':state.get('counts')})
                return generation
        if process.poll() is not None:
            raise AssertionError(('executor ended before cancel point',process.returncode,state))
        time.sleep(.1)
    raise AssertionError(('phase not reached',state))

def cancel(process):
    code,result=invoke('cancel','--manifest',str(manifest))
    assert code==0,result
    events.append({'cancel_response':result['data']})
    assert process.wait(timeout=30)==3
    state=status()
    assert state['terminal']=='cancelled' and state['terminal_confirmed'],state
    events.append({'terminal':'cancelled','counts':state.get('counts')})
    return state

try:
    requests=[]
    for frames in (3,48,3):
        requests.append({'input_blend':str(source),'target_object':target,'preset_id':'rigid_drop_pack',
                         'bake':True,'verify_cold_reopen':True,'parameters':{
                             'frame_end':frames,'scatter_count':0,'particle_radius':.2,
                             'wind_vector':[0,0,0],'wind_turbulence':0,'rest_length':0,'stiffness':0}})
    request=OUT/'batch.json'
    request.write_text(json.dumps({'items':requests}),encoding='utf-8')
    process=start('batch','batch','--request',str(request),'--jobs-dir',str(OUT/'jobs'),
                  '--timeout-seconds','120','--memory-limit-mb','2048')
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        plans=list((OUT/'jobs').glob('*/resume_plan.json'))
        if plans:
            manifest=plans[0];break
        if process.poll() is not None:raise AssertionError('batch ended before manifest')
        time.sleep(.1)
    assert manifest is not None
    first_generation=wait_phase(process,1)
    cancel(process)
    plan=json.loads(manifest.read_text(encoding='utf-8'))
    assert [c['status'] for c in plan['candidates']]==['success','failed','pending'],plan
    first_job=plan['candidates'][0]['attempts'][0]['job_id']

    process=start('resume-cancel','resume','--manifest',str(manifest),'--timeout-seconds','120')
    second_generation=wait_phase(process,1,first_generation)
    cancel(process)
    process=start('resume-finish','resume','--manifest',str(manifest),'--timeout-seconds','120')
    assert process.wait(timeout=180)==0
    result=json.loads((OUT/'resume-finish.stdout.log').read_text(encoding='utf-8'))
    assert result['ok'] and result['data']['skipped']==1 and result['data']['rerun']==2,result
    state=status()
    assert state['terminal']=='success',state
    plan=json.loads(manifest.read_text(encoding='utf-8'))
    assert [c['status'] for c in plan['candidates']]==['success']*3
    assert [len(c['attempts']) for c in plan['candidates']]==[1,3,1]
    assert plan['candidates'][0]['attempts'][0]['job_id']==first_job
    for candidate in plan['candidates']:
        success=next(a for a in candidate['attempts'] if a['attempt']==candidate['success_attempt'])
        assert success['cold_reopen']['evidence_schema']=='physics-geometry-evidence-v2',success
    assert hashlib.sha256(source.read_bytes()).hexdigest()==source_sha
    code,repeat=invoke('cancel','--manifest',str(manifest))
    assert code==0 and repeat['data']['terminal']=='success',repeat
    (OUT/'result.json').write_text(json.dumps({'pass':True,'manifest':str(manifest),'events':events,
        'generations_differ':first_generation!=second_generation,'attempt_counts':[1,3,1],
        'source_unchanged':True,'final_status':state,'schema':'physics-geometry-evidence-v2'},indent=2),encoding='utf-8')
    print('LIVE_BATCH_CANCEL_RESUME_V04_PASS')
finally:
    for process,stdout,stderr in owned:
        if process.poll() is None:
            if manifest is not None:
                try:invoke('cancel','--manifest',str(manifest))
                except Exception:pass
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # This is the exact Popen owned by this test. Its Job Object
                # closes on exit and owns only its own worker process tree.
                process.terminate();process.wait(timeout=10)
        stdout.close();stderr.close()
