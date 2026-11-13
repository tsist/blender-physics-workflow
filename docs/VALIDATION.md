# Validation & Explicit Boundaries (v0.1.0)

## Verified Local Baseline

- **Platform**: Windows x64, Blender **5.2.1 LTS** (`build 9e2066aef7ef`), Python **3.12+ / 3.13**.
- **Contract Unit Tests**: 4 tests in `tests/test_physics_workflow_contract.py` (`describe_contract`, parameter normalization & hashing, validation bounds rejection, CLI `describe` envelope).
- **3-Domain End-to-End Acceptance (Local Blender 5.2.1)**:
  1. `rigid_scatter` (`rigid_chain_pendulum`): `GN_NATIVE` build, world-to-local anchor offset, substeps repeat zone, `DISK` bake, cold reopen hash match, multi-frame EEVEE render inspection.
  2. `cloth_rope` (`cloth_banner_wind`): pinned banner with relative-rest PBD displacement blur (`p_pred - pnw_rest_pos`) and wind turbulence, `DISK` bake, cold reopen, multi-frame EEVEE render inspection.
  3. `fluid_emission` (`fluid_fountain_sdf`): particle emission, obstacle deflection, `PointsToSDFGrid` + `GridToMesh` liquid surface reconstruction, `DISK` bake, cold reopen, EEVEE render inspection, and synchronous Alembic (`.abc`) export (`as_background_job=False`).

## Known Non-Goals & Limits

1. `GN_NATIVE` solvers are modular Geometry Nodes PBD / SDF / particle approximations, **not** bit-exact replacements for C++ Bullet Rigid Body, XPBD Cloth C++ internals, or Mantaflow FLIP solvers when strict momentum/volume conservation is required.
2. Bridge modes (`BULLET_BRIDGE`, `XPBD_ASSET_BRIDGE`, `MANTAFLOW_BRIDGE`) wrap native Blender modifiers/worlds for compatibility, and detailed multi-scene cross-version benchmarking outside Blender 5.2.1 LTS is not claimed.
3. GitHub Actions CI executes host-only Python syntax checks, SHA-256 inventory checks, unit tests, `cli.py describe`, and deterministic archive generation on Windows and Ubuntu; it does **not** run live Blender rendering in cloud CI.
