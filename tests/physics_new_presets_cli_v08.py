"""Public batch/cold/resume path for elastic shells and planar tipping boxes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1]).resolve()
source = out / 'input.blend'
before = hashlib.sha256(source.read_bytes()).hexdigest()
os.environ.update(PHYSICS_WORKFLOW_ADDON_DIR=str(ROOT / 'tools/physics_nodes_workflow_addon'),
                  BLENDER_USER_CONFIG=str(out / 'config'))
items = []
for name, preset in [('BoxInput', 'rigid_box_tipping'), ('ShellInput', 'softbody_elastic_shell')]:
    items.append({'input_blend': str(source), 'output_blend': str(out / (name + '.blend')),
                  'target_object': name, 'preset_id': preset,
                  'bake': True, 'verify_cold_reopen': True,
                  'bake_directory': str(out / (name + '-cache')),
                  'parameters': {'frame_start': 1, 'frame_end': 120}})
request_file = out / 'request.json'
request_file.write_text(json.dumps({'items': items}, indent=2), encoding='utf-8')

def call(label, args):
    p = subprocess.run([sys.executable, str(ROOT / 'tools/physics_workflow_cli_v1/cli.py'), *map(str, args)],
                       capture_output=True, text=True, encoding='utf-8', timeout=300)
    (out / (label + '.json')).write_text(p.stdout, encoding='utf-8')
    (out / (label + '.log')).write_text(p.stderr, encoding='utf-8')
    data = json.loads(p.stdout)
    assert p.returncode == 0 and data['ok'], data
    return data

result = call('batch', ['batch', '--request', request_file, '--jobs-dir', out / 'j',
                        '--timeout-seconds', '240', '--memory-limit-mb', '4096'])
manifest = Path(result['data']['resume_plan'])
plan = json.loads(manifest.read_text(encoding='utf-8'))
assert [row['status'] for row in plan['candidates']] == ['success', 'success']
resumed = call('resume', ['resume', '--manifest', manifest, '--timeout-seconds', '240'])
assert resumed['data']['skipped'] == 2 and resumed['data']['rerun'] == 0, resumed
assert hashlib.sha256(source.read_bytes()).hexdigest() == before
(out / 'result.json').write_text(json.dumps({'ok': True, 'manifest': str(manifest),
    'source_sha256_unchanged': before, 'skipped': 2, 'rerun': 0}, indent=2), encoding='utf-8')
print('NEW_PRESETS_CLI_PASS')
