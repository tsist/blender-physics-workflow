# Physics workflow contract — 0.8.1-dev.2

`BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1`, request schema_version 1.0. Query `cli.py describe` for parameter defaults, limits and capabilities.

Operations: build, update, parameters, bake, invalidate_cache, inspect, adopt, export. Domains: rigid_scatter, cloth_rope, fluid_emission. GN_NATIVE is implemented. BULLET_BRIDGE, XPBD_ASSET_BRIDGE and MANTAFLOW_BRIDGE are reserved and return UNSUPPORTED_BRIDGE.

UI Build protects the source by default. Parameter changes retain authored graphs; explicit Rebuild creates replacement graphs and retains old wrappers. All domains accept single/collection mesh colliders in world space. Prototype normalization centers the bbox and scales its half diagonal to the sphere radius; it preserves visible shape/materials but realizes internal instances. Bending and tearing require a cloth_rope Mesh input. Numeric inputs must be finite and integer parameters integral.

## Simple animated colliders

The supported subset is ordinary mesh colliders driven by location, rotation_euler, rotation_quaternion or scale keyframes in their currently bound Action slot. Signatures include curves, handles/interpolation/easing, mute/extrapolation, binding, relevant animation settings, static transform channels and static parent transforms. Signing must not change frame/subframe/selection. Unknown dynamic dependencies must be rejected rather than marked cache-valid. Target animation, animated prototypes, NLA, drivers, constraints, shape-key deformation and dynamic modifier/data dependencies are outside this subset.

## Output-state evidence

`physics-geometry-evidence-v2` preserves the existing geometry_sha256 algorithm and adds physics_attributes_sha256 and state_sha256. Available top-level Mesh, PointCloud, Curves and INSTANCE `pnw_` attributes are hashed with component, domain, data type, count and ordered values. Instance values are bound to transform and evaluated reference geometry. Realizing first is unsafe because a prototype's same-named attribute can shadow an instance value. Numeric values are quantized to six decimals; non-finite values and unsupported types fail explicitly.

Verifier comparison includes complete coverage declarations and the signed attribute payload, not only digest strings. Old v1 or missing evidence is rejected. Attribute inventory comparison does not imply attribute availability: surface reconstruction may output no pnw fields. Internal solver state and nested-reference physics attributes are excluded. The v1 geometry algorithm does not provide full raw PointCloud/Curves geometry coverage. Inspection marks output velocity separately from position-based estimates.

## Jobs and replay

run/sweep/batch persist `resume_plan.json`, schema pnw.workflow.resume_plan version 1. `status --manifest <absolute path>` reports observed stage, candidate, cancellation and recovery state; it does not invent frame progress. `cancel` writes a generation-bound request and does not signal a PID from the manifest. Only the original executor's resource guard terminates its own process tree. A request is not terminal cancellation. New resume generations ignore old requests.

Original writers and resume share an OS file lock. Successful candidates are skipped only after request/input, receipt/output/export/cache and required current-schema cold-reopen evidence validation. Incomplete or damaged candidates replay from the original input into new jobs. Input or normalized parameter changes reject the plan; invalidate_cache cannot be replayed. This is not arbitrary intermediate-frame recovery. Local Windows locks are qualified; network filesystems and abrupt power loss are not.

## Exports

STATIC_MESH writes a blend with realized static geometry. ALEMBIC writes a realized mesh sequence and accepts empty endpoints but rejects an all-empty range. OPENVDB writes a verified surface level-set from closed finite world meshes; it does not export original smoke density/velocity grids. Output paths must be new. Invalid cache identities are rejected and prior cache generations retained.

Disk exports now require a pnw.export.delivery/1 sidecar at <primary>.pnw-export.json. Runner acceptance, final signing and resume verify its SHA, primary and every external dependency. Old disk exports without this evidence replay. In-scene STATIC_MESH is the only no-sidecar case. See EXPORT-DELIVERY.md for preservation scope.


operation=prepare_instances requires rigid domain and a nonempty local static Mesh instance_collection. It refuses bake/export/cold verification in the same request. Returns prepared_instances with new collection and source/copy mapping. It never rebinds a modifier. The follow-up build uses prepared_instances.prepared_collection. See PROTOTYPES.md.


parameters.activation_frame is a strict JSON integer from 0 to 100000. Zero enables immediate participation; positive values are absolute scene frames. Only rigid_scatter accepts nonzero. Frames beyond frame_end are allowed (wait throughout). The first simulation frame always initializes state. Output pnw_active is a boolean on points/instances. See ACTIVATION.md.


0.8 adds verified cloth pinning, particle population evidence, elastic surface shells, planar box tipping and revised sphere contacts. See docs/STAGE-VALIDATION.md for measured qualification and remaining limits.


0.8.1 separates mesh-to-ground measurements from instance origins, reports unavailable motion as null, and exposes actual solver sleep. See docs/DIAGNOSTICS-VALIDATION.md.
