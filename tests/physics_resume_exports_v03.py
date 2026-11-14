"""Real CLI resume integrity for both new export formats; no solver quality claim."""
import json
import os
from pathlib import Path
import sys
from test_physics_workflow_resume import TestPhysicsWorkflowResume, runner, resume, sha256

TestPhysicsWorkflowResume.setUpClass()
fixture = TestPhysicsWorkflowResume()
results = {}
for fmt, suffix in [('OPENVDB', '.vdb'), ('ALEMBIC', '.abc')]:
    case = fixture.root / fmt.lower()
    case.mkdir()
    request = fixture._base_request()
    request['parameters'].update(scatter_count=1, sdf_voxel_size=.1)
    request.update(export_format=fmt, export_path=str(case/'result')+suffix)
    initial = runner.run_single(request, case/'jobs', timeout_seconds=120, memory_limit_mb=2048)
    assert initial.ok, initial.error
    manifest = Path(initial.data['resume_plan'])
    skip = resume.resume_manifest(manifest, timeout_seconds=120, memory_limit_mb=2048)
    assert skip.ok and skip.data['skipped']==1 and skip.data['rerun']==0, skip
    original = Path(request['export_path'])
    original.write_bytes(original.read_bytes()+b'integrity-test')
    changed_sha = sha256(original)
    replay = resume.resume_manifest(manifest, timeout_seconds=120, memory_limit_mb=2048)
    assert replay.ok and replay.data['rerun']==1 and replay.data['skipped']==0, replay
    reasons = replay.data['candidates'][0]['success_verification']['reasons']
    assert any(r['code']=='export_sha256_mismatch' for r in reasons), reasons
    assert sha256(original)==changed_sha and sha256(fixture.input_blend)==fixture.input_sha
    again = resume.resume_manifest(manifest, timeout_seconds=120, memory_limit_mb=2048)
    assert again.ok and again.data['skipped']==1 and again.data['rerun']==0
    results[fmt] = {'pass':True, 'manifest':str(manifest), 'initial_skip':1,
                    'damaged_rerun':1, 'final_skip':1, 'original_retained':True}
out=Path(sys.argv[1]).resolve()
out.write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps(results,indent=2))
