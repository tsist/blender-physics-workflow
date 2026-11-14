# 0.8.1 diagnostic measurements

Blender 5.2.1 LTS, build 9e2066aef7ef. This patch changes inspection and UI reporting; solver graphs, v2 geometry/state digest algorithms and cache identities remain unchanged. The 0.8 representative-scale qualification remains scoped by STAGE-VALIDATION.md.

`frames[frame].ground_contact.visible_geometry` measures every evaluated mesh vertex, including depsgraph mesh instances, against the requested world-horizontal plane. Transforms include rotation and negative/nonuniform scale. `instance_origins` reports origins separately. A triangulated sphere's visible mesh can have a small positive gap while its collision sphere touches the ground. This metric is not collision-proxy clearance, arbitrary collider distance, camera visibility, or continuous collision detection. The result reports the depsgraph evaluation mode and unsupported component types. Below-ground counts are vertex counts, using a 1e-4 tolerance.

Legacy `ground_clearance_min` and `ground_penetration_count` retain their mesh-vertex/origin semantics and expose `ground_measurement_scope`. New consumers should use `ground_contact`.

Missing or incomparable position data yields null speed/low-speed fraction, with a reason in `late_motion`. Empty output, one-frame intervals, changed point counts, changed mesh topology and mesh/instance source switches cannot imply rest. Index-paired estimates do not establish identity or continuous stationarity. Invalid or partial velocity attributes do not silently fall back to an estimate. Valid `pnw_velocity` output takes precedence.

`solver_sleep` reads actual boolean `pnw_sleep`, with separate `pnw_active` and `pnw_sleep_rooted` counts when observable. Counts are top-level output point rows, not unique physical bodies. Per-component statuses and counts are retained. Contact convergence validates both attributes and component cardinalities. Neither contact convergence nor low speed implies sleep or general physical quality.

Runtime checks:

- The retained baseline reproduces four misleading reports: empty, changed-count and single-frame false rest, and origin height mislabelled as ground clearance.
- Dedicated Blender fixtures cover surface/origin separation, mixed and nested mesh instances with negative scale and rotation, independent Realize comparisons, generated object prototypes with empty source meshes, non-mesh and Empty outputs, invalid sleep and velocity attributes, nonfinite velocity, point-source changes, contact metadata, unavailable UI values and clearing stale success after failure.
- Public CLI: two sphere proxies, 80-frame managed bake, independent cold verification, inspection at frames 80/1/40/80 without changing state digests, UI inspection, non-target/source protection, resume skip=1/rerun=0.
- Three-domain small-fixture bake/save/cold state comparison and actual inspection operators cover rigid, cloth and particle surface output. Exact extracted packages execute affected checks before delivery.

No claim is made for arbitrary collection hierarchies, camera-visible filtering, hidden instance semantics, all malformed component combinations, cross-platform behavior, or new solver capabilities. No global installation or publication is performed.
