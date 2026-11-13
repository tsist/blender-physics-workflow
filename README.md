# Blender Physics Workflow

[简体中文](README.zh-CN.md) · [Contract Reference](docs/CONTRACT.md) · [Validation & Scope](docs/VALIDATION.md)

Modular **Geometry Nodes** physics workbench add-on (**0.1.0**), isolated companion CLI (**0.1.0**), and portable agent skill (**0.1.0**) for **Blender 5.2.1 LTS** (verified build `9e2066aef7ef`, GPL-3.0-or-later).

Companion integration PR target: [tsist/blenderctl](https://github.com/tsist/blenderctl).

---

## Architecture Overview

1. **Wrapper (`PNW_Wrapper_<obj>_<mod>`)**: Modifier-facing interface group preserving user-inserted nodes (`User_Added_*`) and extra group sockets across parameter updates, rebuilds, and disk bakes.
2. **Core (`PNW_Core_<obj>_<mod>`)**: One root `GeometryNodeSimulationInput` / `GeometryNodeSimulationOutput` + trailing `GeometryNodeBake` + internal `GeometryNodeRepeatInput` / `GeometryNodeRepeatOutput` (`substeps = 1..8`, `dt_sub = Delta Time / Substeps`), guaranteeing a 1:1 binding between one modifier and one `DISK` bake cache.
3. **12 Modular Stateless `PN_*` Subgroups**:
   - Forces: `PN_Force_GravityDrag`, `PN_Force_WindTurbulence`
   - Collision: `PN_Collide_GroundPlane`, `PN_Collide_MeshSurface`
   - Constraints: `PN_Constraint_PinAnchor`, `PN_Constraint_DistanceChain`
   - Solvers: `PN_Solver_RigidScatterStep`, `PN_Solver_ClothRopeStep` (relative-rest displacement PBD blur + velocity coupling), `PN_Solver_FluidParticleStep`
   - Post-processing: `PN_Post_InstanceRigid`, `PN_Post_FluidSDFMesh` (`PointsToSDFGrid` -> `GridToMesh`), `PN_Post_SmokeVolume` (`PointsToVolume` -> `VolumeToMesh`)

---

## Supported Domains, Presets & Bridge Modes

- **Contract**: `BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1` (`schema_version: 1.0`)
- **Domains & Presets**:
  - `rigid_scatter`: `rigid_drop_pack`, `rigid_chain_pendulum`
  - `cloth_rope`: `cloth_banner_wind`, `rope_catenary_swing`
  - `fluid_emission`: `fluid_fountain_sdf`, `smoke_plume_volume`
- **Operations**: `build`, `update`, `parameters`, `bake`, `invalidate_cache`, `inspect`, `adopt`, `export`
- **Bridge Modes**: `GN_NATIVE` (default), `BULLET_BRIDGE`, `XPBD_ASSET_BRIDGE`, `MANTAFLOW_BRIDGE`
- **Export Formats**: `NONE`, `STATIC_MESH`, `ALEMBIC`, `OPENVDB`
- **N-Panel UI**: `3D Viewport -> N-Panel -> Physics Nodes` (`VIEW_3D_PT_physics_nodes_workbench`)

---

## Quick Start

### 1. Host Verification & Contract Describe

Requires Python **3.12+**:

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
python -B tools/physics_workflow_cli_v1/cli.py describe
```

### 2. Running Simulations via CLI (Blender 5.2.1 LTS Required)

```powershell
$env:BLENDER_PATH = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
python -B tools/physics_workflow_cli_v1/cli.py run `
  --request examples/fluid_fountain_sdf.json `
  --jobs-dir ./runtime/jobs `
  --timeout-seconds 300
```

CLI subcommands: `describe`, `run`, `sweep`, `batch`.

### 3. Installing the Add-on & Skill

- **Blender Add-on**: Download `physics_nodes_workflow_addon-0.1.0.zip` from [Releases](https://github.com/tsist/blender-physics-workflow/releases) and install via **Preferences -> Extensions -> Install from Disk**.
- **Agent Skill**: Download `blender-physics-workflow-skill-0.1.0.zip` or copy `skills/blender-physics-workflow/` into your agent skills directory.

---

## Verification & Release Checks

```powershell
python -B scripts/verify_release.py
python -B scripts/build_release.py --output ./dist
```

Local Blender 5.2.1 LTS Windows verification covers unit contract tests, extension packaging validation (`blender --command extension validate`), 3-domain `GN_NATIVE` simulation & `DISK` bake + cold reopen, multi-frame EEVEE renders, and Alembic `.abc` export (`fluid_fountain_sdf`). Host-only GitHub Actions CI checks syntax, inventory hashes, contract unit tests, CLI `describe`, and deterministic ZIP builds without running Blender.

See [Validation & Scope](docs/VALIDATION.md) for explicit verified bounds and non-goals.
