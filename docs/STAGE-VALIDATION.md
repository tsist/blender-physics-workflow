# 0.8 representative-stage qualification

Test environment: Blender 5.2.1 LTS, build `9e2066aef7ef`, Windows. This is a development candidate. Qualification applies to the described scenes and input boundaries; it is not a general guarantee for arbitrary simulation geometry.

## Verified independent paths

- Cloth: a 10,000-vertex hanging flag, wind and collider interaction, 120 frames, 8 substeps and 64 constraint iterations. Native zero mass represents pinned points; edge tolerance scales with rest-edge length. Full-frame physical observations, independent cache replay, two views and the complete rendered sequence passed. Lower-iteration failures remain documented; default settings do not promise equivalent quality for every mesh.
- Emission: 52,520 actual simulated particles over 250 frames, surfaced to mesh. Two separately baked runs with the same seed, cold replay, complete sequence and multiple views passed. `pnw_particle_count` reports simulated population on the reconstructed surface; surface vertex count is not particle count. Internal particle velocity is not inferred from this field.
- Elastic shell: `softbody_elastic_shell`, 42 vertices, 120 frames. A closed triangulated surface deforms locally and recovers after contact. Topology, finite state, positive bounded volume, source protection and independent cache replay passed. The tested configuration includes bending. It is not a volumetric tetrahedral soft-body, pressure or self-collision solver. Stiffness does not promise monotonic whole-shape deformation.
- Tipping: `rigid_box_tipping`, unit mass, XZ translation and Y rotation, true-size boxes on a fixed horizontal plane. Contact normal/tangent impulses and inertia drive angular velocity. Critical-angle comparisons and 120-frame bake/cold/sequence passed. Every raw mesh vertex produces one box, maximum 64; box-box contact and arbitrary collider inputs are unsupported. Single-point support can rock or creep and is not qualified as exact static rest.
- Visible workbench: actual mouse Build, Bake, Inspect, box half-extent edit, new Bake, Inspect and Save were followed by an independent process reading valid direct cache in nonchronological frame order. The source remained unmodified. Hidden settings from another preset no longer prevent the box workflow; angle endpoints are safe through the UI float representation.
- New presets through public CLI: both presets completed a 120-frame batch with cold verification, a 4 GiB process budget and 240-second per-operation timeout. Resume skipped both verified candidates; non-target objects and source bytes were protected. Native GUI installation and external publication are separate actions.

## Sphere contact and load qualification

The final integrated source passed a 200-sphere container/ramp scene with frame-12 activation and 250 frames (4 substeps, 3 base contact iterations, radius 0.2, restitution 0.35, friction 0.45, sleep threshold 0.08). In frames 230–250, maximum overlap was 8.35e-8 and position, velocity and rotation changes were zero, within the unchanged 1e-4 criterion. All frame samples stayed inside the container; minimum mesh distance was 0.199999809. Incoming negative and outgoing positive ramp-normal velocities confirmed rebound.

A separate sparse 2000-sphere scene completed 250 frames with finite output, preserved count and activation, zero late overlap/speed/displacement. The observed bake times were 98.38 seconds for the container scene and 51.33 seconds for the sparse load, with concurrent test activity; these are measurements, not throughput guarantees. The build processes peaked at approximately 410 MiB and 790 MiB working set respectively (GPU memory not measured).

Both outputs passed independent nonchronological reopen and all 250 rendered-frame state comparisons. Two camera views at frames 12/30/250, all consecutive frame sheets, and complete 250-frame video decode were checked. No gross disappearance, explosion or discontinuity was observed; image review does not establish subframe collision guarantees.

The solver separates projection support from load-bearing sleep roots. Public-entrypoint counterexamples confirm that touching or slightly separated low-speed free pairs continue free fall, and sleeping three-layer stacks wake immediately after an animated platform moves away. Additional isolated tests cover support removal/tilt, side-wall pair spacing, wind, chain and non-downward gravity. Sleep is restricted to downward gravity. General independent-contact-island tracking and support motion preserving all detection signals are not qualified.

The retained 27-sphere regression now passes the original 1e-4 overlap threshold (maximum approximately 9.99e-6), but its three-iteration case still has late speed around 0.119; it is not a universal static-rest claim. Historical failures remain intact. The 2000-body sparse load is not evidence for a 2000-body dense pile.

## Reused pipeline evidence

Previous verified paths retain explicit source copies, author-edited node links, managed cache generations, semantic invalidation, independent reopen, cancellation followed by candidate replay, batch/sweep isolation, static mesh/Alembic/OpenVDB delivery manifests and explicit prototype preparation. New presets reuse the same public core. Archive acceptance requires affected public entrypoints to run from extracted source and every runtime byte to match the package; the separate package-check record reports that final result.

Native Bullet/Mantaflow adapters remain explicitly reserved interfaces, as specified. Full incompressible liquid, native smoke density grids, volumetric solid soft bodies, arbitrary three-dimensional box contact, per-point activation, cross-modifier coupling, arbitrary intermediate-frame continuation and real power-loss recovery are not claimed. Alembic retains face sets and tested UV maps but not shaders/images; exported level-set VDB is not a smoke density field.

## Reproduction

Use `tests/blender_physics_stage_rigid_v08.py` with `-- build <new-directory> small|large`, followed by `cold`, `render`, `render2` or `playback` using the same directory. The small scene and sparse large load are separate qualifications. `tests/blender_physics_stage_rigid_observe_v08.py` checks the small scene's container/ramp contact and late rotation/speed observations.

For new presets, run `tests/blender_physics_new_presets_v08.py -- prepare <new-directory>` under Blender, then `tests/physics_new_presets_cli_v08.py <directory>` under Python, then the Blender script with `-- observe <directory>`. Configure `BLENDER_PATH` to the verified installation. The current CLI includes its runtime helpers and does not require a separate shared core checkout. Do not overwrite existing evidence directories.
