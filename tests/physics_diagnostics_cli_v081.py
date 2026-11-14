"""Actual public CLI bake/cold/resume plus UI inspection of the saved artifact.

Host: python FILE NEW_OUTPUT_DIR. BLENDER_PATH and shared core may be supplied.
The Blender child reuses this file with -- prepare|observe OUTPUT_DIR.
"""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if '--' in sys.argv:
    import bpy
    mode, folder = sys.argv[sys.argv.index('--') + 1:]
    out = Path(folder).resolve()
    sys.path.insert(0, str(ROOT / 'tools'))
    import physics_nodes_workflow_addon as addon
    from physics_nodes_workflow_addon import contract, geometry_evidence, inspection
    addon.register()
    if mode == 'prepare':
        bpy.ops.wm.read_factory_settings(use_empty=True)
        mesh = bpy.data.meshes.new('DropPoints')
        mesh.from_pydata([(0, 0, .8), (1, 0, 1.2)], [], [])
        obj = bpy.data.objects.new('DropPoints', mesh)
        bpy.context.scene.collection.objects.link(obj)
        sentinel = bpy.data.objects.new('Untouched', None)
        sentinel.location = (7, 8, 9)
        bpy.context.scene.collection.objects.link(sentinel)
        bpy.ops.wm.save_as_mainfile(filepath=str(out / 'input.blend'))
    else:
        bpy.ops.wm.open_mainfile(filepath=str(out / 'output.blend'), use_scripts=False)
        request = json.loads((out / 'request.json').read_text(encoding='utf-8'))
        expected = json.loads((out / 'run.json').read_text(encoding='utf-8'))['data']['worker']['inspection']
        obj = bpy.data.objects['DropPoints']
        normalized = contract.normalize_request(request)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        obj.pnw_settings.preset_id = request['preset_id']
        for key, value in normalized['parameters'].items():
            if hasattr(obj.pnw_settings, key):
                setattr(obj.pnw_settings, key, value)
        samples = []
        for frame in [80, 1, 40, 80]:
            before = geometry_evidence.sample_frame(obj, frame)
            row = inspection._sample_object_state(obj, frame, 0)
            after = geometry_evidence.sample_frame(obj, frame)
            assert before['state_sha256'] == after['state_sha256']
            if frame == 80:
                for field in ('ground_contact', 'solver_sleep', 'contact_convergence'):
                    assert row[field] == expected['frames']['80'][field], (field, row[field])
                geo = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).evaluated_geometry()
                points = geo.instances_pointcloud()
                raw_sleep = sum(bool(d.value) for d in points.attributes['pnw_sleep'].data)
                assert row['solver_sleep']['sleeping_count'] == raw_sleep
            samples.append({'frame': frame, 'state_unchanged': True,
                            'ground_contact': row['ground_contact'], 'solver_sleep': row['solver_sleep']})
        assert bpy.ops.pnw.inspect_workflow() == {'FINISHED'}
        summary = obj.pnw_settings.last_inspect_summary
        assert summary.startswith('Diagnostics valid'), summary
        assert 'Mesh to ground plane:' in summary and 'Solver sleeping:' in summary
        assert 'Low-speed fraction:' in summary and '| Rest:' not in summary
        assert tuple(bpy.data.objects['Untouched'].location) == (7, 8, 9)
        (out / 'observe.json').write_text(json.dumps({'ok': True, 'frames': samples,
            'ui_summary': summary, 'non_target_unchanged': True}, indent=2), encoding='utf-8')
    addon.unregister()
else:
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    blender = Path(os.environ.get('BLENDER_PATH', ROOT / 'runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe'))
    os.environ.update(PHYSICS_WORKFLOW_ADDON_DIR=str(ROOT / 'tools/physics_nodes_workflow_addon'),
                      BLENDER_USER_CONFIG=str(out / 'config'))
    commands = []
    def run(label, args):
        p = subprocess.run(list(map(str, args)), capture_output=True, encoding='utf-8', timeout=300)
        (out / (label + '.log')).write_text(p.stdout + '\n' + p.stderr, encoding='utf-8')
        commands.append({'label': label, 'exit_code': p.returncode})
        assert p.returncode == 0, (label, p.stdout[-3000:], p.stderr[-1500:])
        print(label + ' passed', flush=True)
        return p.stdout
    base = [blender, '--background', '--factory-startup', '--disable-autoexec', '--python-exit-code', '1',
            '--python', Path(__file__).resolve(), '--']
    run('prepare', base + ['prepare', out])
    source_sha = hashlib.sha256((out / 'input.blend').read_bytes()).hexdigest()
    request = {'input_blend': str(out / 'input.blend'), 'output_blend': str(out / 'output.blend'),
        'target_object': 'DropPoints', 'preset_id': 'rigid_drop_pack', 'bake': True,
        'verify_cold_reopen': True, 'bake_directory': str(out / 'cache'),
        'parameters': {'frame_end': 80, 'scatter_count': 0, 'particle_radius': .12,
                       'wind_turbulence': 0, 'wind_vector': [0, 0, 0], 'restitution': 0,
                       'stiffness': 0, 'rest_length': 0}}
    (out / 'request.json').write_text(json.dumps(request, indent=2), encoding='utf-8')
    cli = [sys.executable, ROOT / 'tools/physics_workflow_cli_v1/cli.py']
    raw = run('run', cli + ['run', '--request', out / 'request.json', '--jobs-dir', out / 'j', '--timeout-seconds', '240'])
    result = json.loads(raw)
    (out / 'run.json').write_text(raw, encoding='utf-8')
    assert result['ok'], result
    inspect = result['data']['worker']['inspection']
    row = inspect['frames']['80']
    ground = row['ground_contact']
    assert ground['visible_geometry']['status'] == 'observed', ground
    assert ground['instance_origins']['clearance_min'] - ground['visible_geometry']['clearance_min'] > .1
    assert ground['visible_geometry']['clearance_min'] >= -1e-4
    assert row['solver_sleep']['status'] == 'observed' and row['solver_sleep']['count'] == 2
    run('observe', base + ['observe', out])
    resumed = json.loads(run('resume', cli + ['resume', '--manifest', result['data']['resume_plan'], '--timeout-seconds', '240']))
    assert resumed['ok'] and resumed['data']['skipped'] == 1 and resumed['data']['rerun'] == 0
    assert hashlib.sha256((out / 'input.blend').read_bytes()).hexdigest() == source_sha
    (out / 'result.json').write_text(json.dumps({'ok': True, 'commands': commands,
        'source_unchanged_sha256': source_sha, 'resume_skipped': 1, 'resume_rerun': 0,
        'end_frame': row, 'sampling': inspect['sampling']}, indent=2), encoding='utf-8')
    print('PHYSICS_DIAGNOSTICS_CLI_PASS')
