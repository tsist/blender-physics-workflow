# Blender Physics Workflow — 0.8.1-dev.1

[简体中文](README.zh-CN.md) · [Download](https://github.com/tsist/blender-physics-workflow/releases/tag/v0.8.1-dev.1) · [Contract](docs/CONTRACT.md)

An editable Geometry Nodes physics workbench, a workflow skill for AI assistants, and an optional CLI. Tested with **Blender 5.2.1 LTS, build 9e2066aef7ef**. This is a development prerelease under **GPL-3.0-or-later**.

## What it does

- Sphere-proxy rigid bodies, cloth and ropes, particle emission with surface reconstruction, elastic surface shells, and planar box tipping.
- Editable node groups, protected source copies, authored-graph preservation, disk bake, inspection, and static mesh / Alembic / surface OpenVDB export.
- CLI parameter sweeps, batches, status, cancellation, and candidate-level resume with output/dependency checks.
- Inspection distinguishes mesh-to-ground distance from instance origins, reports unavailable motion explicitly, and reads actual solver sleep state.

The qualified scenes include a 200-body container, 2000 sparse rigid bodies, 10000 cloth vertices, and 52520 simulated particles. These are scoped examples, not universal performance or physical-quality guarantees. See [stage validation](docs/STAGE-VALIDATION.md) and [diagnostic validation](docs/DIAGNOSTICS-VALIDATION.md).

Particles are not a pressure/incompressible liquid solver. Elastic shells are not volumetric soft bodies. Box tipping is restricted to XZ translation, Y rotation and a fixed horizontal ground, without box-to-box contact. Bullet and Mantaflow bridges are reserved interfaces.

## Install the Blender add-on

1. Download `physics_nodes_workflow_addon-0.8.1-dev.1.zip` from the release page.
2. In Blender, open **Edit → Preferences → Get Extensions → menu → Install from Disk**, then choose that ZIP. Do not select the source or skill archive.
3. Enable **Physics Nodes Workbench**. Select a target object and open the **Physics Nodes** tab in the 3D View sidebar (`N`).
4. Choose a preset and use **Build / Update**. Source protection creates a working copy by default. Use **Bake to Disk**, then **Inspect Simulation** and save the project.

Keep the managed cache directories with the saved project. Upgrading an older graph can require an explicit **Rebuild** on a working copy; rebuilding retains the old graph. Back up existing projects before migrating from v0.1.0. Installation alone does not rebuild old graphs or make old caches valid.

## Use the companion skill

Download `blender-physics-workflow-skill-0.8.1-dev.1.zip` and extract its `blender-physics-workflow` directory into your assistant's skills directory. For Codex, this is normally `$CODEX_HOME/skills` or `~/.codex/skills`. The skill is also available at [skills/blender-physics-workflow/SKILL.md](skills/blender-physics-workflow/SKILL.md).

The skill ZIP contains instructions, not executable tools. Also clone this repository or extract `blender-physics-workflow-0.8.1-dev.1-source.zip`. Tell your assistant that location: `<REPO_ROOT>` in the skill means that source directory, and its `docs/` references are relative to that directory. The license is included inside the copied skill folder. The skill does not install Blender or the add-on automatically.

## Optional CLI

The add-on and this CLI are self-contained; a separate blenderctl checkout is not required. Use Python 3.12 or 3.13 and set `BLENDER_PATH` to the verified Blender executable. Run these commands from the source directory.

```powershell
$env:BLENDER_PATH = 'C:/path/to/blender.exe'
python -B tools/physics_workflow_cli_v1/cli.py describe
python -B tools/physics_workflow_cli_v1/cli.py run --request request.json --jobs-dir ./jobs
python -B tools/physics_workflow_cli_v1/cli.py resume --manifest C:/jobs/run-id/resume_plan.json
```

See the [skill](skills/blender-physics-workflow/SKILL.md) for request examples and the [contract](docs/CONTRACT.md) for supported operations. Cancel requests termination; the owning process confirms the final state. Resume validates completed candidates and replays incomplete ones from the original input, not from an arbitrary simulation frame.

## Validation and reproducible archives

```powershell
python -B scripts/verify_release.py
python -B -m unittest discover -s tests -p "test_*.py" -v
python -B scripts/build_release.py --output ./dist
```

The release contains the add-on ZIP, source/CLI ZIP, skill ZIP and `SHA256SUMS`. Each ZIP includes the license. Host CI checks source, contracts and deterministic packaging on Windows and Linux; Blender-dependent tests require the stated local environment and skip when it is absent. Actual physics and UI qualification is documented separately in [validation](docs/VALIDATION.md).
