# Changelog

## 0.8.1-dev.1

Separate evaluated mesh-to-ground measurements from instance origins. Report unavailable motion as null for empty, single-frame or incomparable output. Observe real solver sleep and validate velocity/contact attributes per component. Clear stale UI success after a failed inspection. Existing 0.8 caches remain readable in the verified compatibility case.

Publish the add-on, portable skill and optional CLI together as a GPL-3.0-or-later development prerelease. All ZIPs include the license, skill versions match the release, and bilingual installation/migration instructions are included. Host checks compare the implementation version with the actual extension manifest.

## 0.8.0-dev.1

Improve sphere contact response and supported sleep/wake behavior. Correct cloth pinning, expose actual simulated particle counts, and add elastic surface shells and planar box tipping. Qualify selected representative loads, cold replay and visible workbench interaction; see STAGE-VALIDATION.md for scope and remaining limits.

## 0.7.0-dev.1

Add whole-workflow rigid activation at a scene frame, with shared UI/CLI/cache identity and explicit retained graph migration.

## 0.6.0-dev.1

Add editable prototype snapshots preserving effective object material slots and native instance ordering, with source protection and dependency identity.

## 0.5.0-dev.1

Preserve target material assignments and multiple UV maps in supported exports. Add signed export delivery manifests and dependency-aware resume.

## 0.4.0-dev.1

Added frame-stable signatures for simple keyframed mesh colliders, including actual bound Action slots and static parent channels. Unknown dynamic dependencies and non-finite animation values fail explicitly.

Added CLI status and cooperative cancellation for run/sweep/batch/resume. The original executor owns process-tree termination; terminal publication and new-generation ownership use the same OS lock. Cancellation before the first candidate returns failure. Resume requires the current cold verifier evidence schema and preserves verified successful candidates.

Added v2 output evidence with actual top-level physical attributes and prototype assignment, without realizing away instance values. Rejects missing/forged coverage and legacy evidence. Inspection identifies speed sources and unavailable fields. Fixed unchanged CLI values being flagged as stale after float32 UI display conversion.

## 0.3.0-dev.1

Added default protected copies, retained-graph rebuild, native mesh bending/tearing, collection colliders across three domains, prototype normalization, verified VDB surface export, realized Alembic sequence output and candidate replay. Fixed collision field reevaluation, nearest-face switches, stale RNA after file reload and empty-endpoint Alembic rejection.

## 0.2 development repair

Repaired cache ownership and semantic identity, source protection, world-coordinate evaluation, cold-reopen verification, static exports and bounded resource handling. This was a local development repair after the initial v0.1.0 publication.
