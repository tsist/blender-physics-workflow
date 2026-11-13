# Physics Workflow Contract (`BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1`)

- `schema_version`: `"1.0"`
- `operations`: `build`, `update`, `parameters`, `bake`, `invalidate_cache`, `inspect`, `adopt`, `export`
- `domains`: `rigid_scatter`, `cloth_rope`, `fluid_emission`
- `bridge_modes`: `GN_NATIVE`, `BULLET_BRIDGE`, `XPBD_ASSET_BRIDGE`, `MANTAFLOW_BRIDGE`
- `export_formats`: `NONE`, `STATIC_MESH`, `ALEMBIC`, `OPENVDB`

## Parameter Bounds

- `substeps`: integer `1..8`
- `frame_start <= frame_end`
- `gravity`: 3-element float vector `[gx, gy, gz]`
- `wind_vector`: 3-element float vector `[wx, wy, wz]`
- `anchor_pos`: 3-element float vector `[ax, ay, az]`
- `restitution`, `drag`, `friction`, `stiffness`: bounded floats (validated by `contract.normalize_request`)
- Windows short bake path protection: parent directory `<= 247` chars, file path `<= 259` chars.
- State metadata persisted per object/modifier in `obj[f"pnw_state_json__{mod.name}"]` with deterministic `parameters_hash` invalidation when simulation parameters change.

## CLI Subcommands (`tools/physics_workflow_cli_v1/cli.py`)

- `describe`: Outputs the JSON contract, presets, domains, bounds, and resolved `blender_binary` / `addon_directory`.
- `run --request <json> --jobs-dir <dir> [--timeout-seconds 300]`: Executes a single isolated Blender job, optionally baking to `DISK`, performing cold-reopen verification, and exporting `STATIC_MESH`/`ALEMBIC`/`OPENVDB`.
- `sweep --request <json> --jobs-dir <dir>`: Evaluates parameter variants against a base scene.
- `batch --request <json> --jobs-dir <dir>`: Executes multiple scene/request entries in isolated job directories.
