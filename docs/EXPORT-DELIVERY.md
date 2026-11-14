# Export delivery contract — 0.5.0-dev.1

Disk results return delivery_manifest={path, sha256, schema:"pnw.export.delivery/1"}.
The UTF-8 JSON sidecar contains schema, export_format, primary={path,sha256,size}, files=[{path,sha256,size,role}], preservation. All paths are absolute. files excludes primary, sidecar and resources packed in the blend. Existing primaries/sidecars are refused. A failed operation may leave its new primary/partial sidecar for diagnosis, but does not return valid delivery evidence.

STATIC_MESH copies the scene with a separate realized target. Effective evaluated OBJECT slots are materialized onto its mesh; face assignments, multiple UVs and active/render selection are preserved in the tested case. Target material graphs and their FILE/packed images are covered. Relative paths are remapped when saving the copy. External files stay at the recorded path: this is not a portable packed bundle or full-scene dependency closure. Linked material/group/image data, external IES/OSL files, unsaved image edits, missing images, UDIM/sequences/movies are explicitly rejected. Custom graphs that already realized prototypes are checked only as evaluated target geometry.

ALEMBIC appends a temporary Realize modifier to the target, exports it with uvs/face_sets enabled, then removes it. Blender reimport retains multiple UV maps and face-set material names, but not shader graphs, images, active/render UV selection or instance identity. Other readers are unverified. OPENVDB remains a verified surface level set; material/UV preservation is not applicable.

Known prototype limit: shared-Mesh prototype OBJECT overrides are lost inside GN realization (with or without normalization). Standard managed prototype inputs now refuse a faithful static/ABC delivery for that case. Authors may explicitly prepare separate Mesh DATA-material prototypes. No automatic source change or hidden adapter is claimed.

Runner acceptance, final signing and resume all verify delivery evidence. Missing/changed external files invalidate prior success. Final-signing failure propagates to CLI and job status. Replay uses a new output path and preserves old delivery files; it cannot recreate a missing external input.

0.6 provides the explicit prepare_instances authoring adapter described in PROTOTYPES.md. Raw incompatible OBJECT overrides remain refused.
