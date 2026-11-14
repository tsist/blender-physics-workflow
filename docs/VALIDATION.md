# Validation — 0.8.1-dev.1

Verified environment: Windows x64, Blender 5.2.1 LTS build 9e2066aef7ef. These cases qualify workflow behavior, not the whole product's physical or visual quality.

| Area | Evidence and observed behavior |
|---|---|
| Output attributes | 14 RNA types; bytes with invalid UTF-8; non-finite rejection; unchanged geometry with changed velocity rejected by an independent Blender verifier |
| Instance assignment | Prototype attribute shadowing avoided; exchanging positions or different prototypes among co-located instances changes physics/state digests while the geometry multiset can stay unchanged |
| Evidence contract | Missing/legacy v1 evidence and forged coverage rejected; cold verifier must itself declare current v2 before successful candidates can be skipped |
| Managed state replay | Rigid, cloth and reconstructed particle surface: 24-frame bake/save/cold, replay order 24/1/12/24; rigid/cloth output pnw attributes match; reconstructed surface declares no pnw attributes |
| UI inspection | Actual bpy inspection operator reports attribute availability and speed source; CLI-to-panel float32 display round trip no longer invalidates unchanged parameters; a real gravity edit still produces invalid diagnostics |
| Animated colliders | Ordinary keyframed mesh transforms through single/collection inputs; animated plane raises sampled sphere to z=3.2 versus static z=1.2 at frame 24; independent cold replay matches |
| Animation identity | Bound Action slot and curve/handle/interpolation, parent and static-channel changes invalidate; signing preserves frame/subframe/selection; NaN static/animated transforms fail closed |
| Cancellation | Real worker and cold-verifier cancellation; batch second candidate cancellation, cancellation during resume, then completion; successful first candidate preserved; sweep cancellation/replay also qualified |
| Ownership and status | Only the owning Job Object signals its tree; foreign PID fields are ignored; request is distinguished from confirmed cancellation; old requests cannot cancel a new generation; old terminal publisher cannot overwrite a new owner |
| Host regression | 33 relevant contract, control, resume, original-writer lock and bounded atomic-replace tests passed on Windows |

The retained 0.3 evidence covers source-copy protection, authored graph preservation, all-domain collection collisions, prototype normalization, bending/tearing, real surface VDB readback, realized Alembic reimport (including empty endpoints), and export/cache integrity during replay. Version 0.5 rechecked the changed export paths; unrelated 0.4 evidence remains applicable.

Reproducers are under tests. `blender_physics_state_roundtrip_v04.py` accepts explicit add-on and CLI paths for package qualification; `physics_job_control_live_v04.py` accepts an immutable small input fixture and candidate paths. Exact archive validation and actual candidate execution are separate checks.

## Limits

Historical 0.7 status: the 27-sphere residual and representative load/UI gates were incomplete. Version 0.8 resolves the retained overlap regression and qualifies the selected 200/2000-body, 10000-vertex cloth and 52520-particle scenes, planar box tipping, elastic surface shells and visible interaction. See STAGE-VALIDATION.md for final measurements and limits; arbitrary dense piles, pressure liquid and volumetric solid soft bodies are not claimed.

Evidence covers available top-level output pnw attributes, not internal solver state or nested prototype physics attributes. Raw PointCloud/Curves geometry is not newly certified by the existing geometry digest. VDB multi-shell orientation/self-intersection, third-party Alembic reader fidelity, NLA/driver/constraint/deforming-collider animation, POSIX cancellation, network-filesystem locks, simultaneous cancel stress and actual power loss remain unqualified. Current RNA collection-read exception branches are defensive but were not reproduced with real RNA.

## 0.5 delivery evidence

34 related host tests passed. Independent Blender cold-read compared target OBJECT material overrides, per-face assignments, two UV maps with different active/render selection, negative scale, shader links and external image pixels. Independent Alembic import compared frames 1/2/3 per-corner world positions, both UV maps and per-face material names. Active/render UV selection was lost on ABC import and is explicitly excluded. Rigid/cloth dynamic geometry and empty endpoint sequences passed independent reimport. Packed images passed cold-read.

Public CLI tests verified skip, same-size image tampering, missing dependency failure, restored dependency replay into a new path and preservation of the old delivery. Twelve additional boundaries cover malformed manifests, schemas, formats, primary paths, invalid sizes, duplicated/self references, nonfinite/deep JSON, in-scene exemption misuse and dependency mutation immediately before final signing.

Shared-Mesh prototypes with different OBJECT overrides lose them during realization in both normalization paths. Exports now reject this known loss; explicitly prepared separate DATA-material meshes preserve Blue/Green in both paths. Custom graphs that already realized hidden prototypes, conflicting implicit UV consumers, full-scene dependency closure and other Alembic readers remain unqualified.


## 0.6 prototype evidence

Blender 5.2.1 LTS: public prepare and UI operator, two normalization modes, separate static save/reopen, prepare-before-build persistence, CLI prepare followed by build/bake/cold verification/Alembic, and independent ABC frames 3/1/2 per-face positions/UV/materials passed. Source/shared mesh, empty slots, two UVs, vertex groups, negative scale and delta transforms were preserved. Mixed natural/case/Unicode/long names retained native prototype assignment. Parent/modifier/constraint/animation/shape-key/nested/nonfinite inputs were rejected; injected creation failure rolled back new IDs, and authored graphs/context remained unchanged. Actual snapshot geometry and DATA/OBJECT slot edits invalidated cache, source edits and shared shader tuning did not. Ten identity checks and four-object cold-open evidence passed separately. Hidden/excluded source combinations, library relocation, other Blender versions and visible mouse UI are unqualified. Full solver/scale gaps from earlier versions remain.


## 0.7 activation evidence

Blender 5.2.1: immediate, delayed, before-start, at-start and beyond-end activation passed at frame_start=5 with fps=30/fps_base=1.25. Delayed motion exactly matched immediate motion shifted by four frames; waiting preserved position/rotation/zero velocity. Deliberately overlapping ground and stretched tether fixtures stayed unchanged until activation, then contact/constraint response occurred. Independent cold replay at 20/5/9/10/11/20 matched full observed state. Actual modifier activation edits invalidated cache. UI Bake applied changed activation; an authored disconnected input remained disconnected and controlled the actual bake. A real 0.6 graph rejected regular Build and retained author edits during explicit replacement. Public CLI sweep releases 8/12 produced different observed active states, and resume skipped both completed candidates. Six activation and five existing contract tests passed after integration. Full load/contact quality, per-point/collision wake, cross-modifier coupling and fractional-frame activation are not qualified.


0.8 adds verified cloth pinning, particle population evidence, elastic surface shells, planar box tipping and revised sphere contacts. See docs/STAGE-VALIDATION.md for measured qualification and remaining limits.


0.8.1 separates mesh-to-ground measurements from instance origins, reports unavailable motion as null, and exposes actual solver sleep. See docs/DIAGNOSTICS-VALIDATION.md.
