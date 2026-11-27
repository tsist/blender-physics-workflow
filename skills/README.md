# Companion Agent Skill

This repository ships the portable agent skill for Physics Nodes Workflow:

- `blender-physics-workflow`: [SKILL.md](blender-physics-workflow/SKILL.md)

The skill documents the Blender 5.3 Native Fluid panel and the standalone Geometry Nodes CLI. Keep a checkout of this repository or extract the matching source ZIP, then tell your assistant its absolute location. `<REPO_ROOT>` means this source directory; `docs/` references are relative to it.

For the standalone skill ZIP, copy its `blender-physics-workflow` folder (including LICENSE) into your agent skills directory. When installing from the repository, copy `skills/blender-physics-workflow` and place a copy of the repository LICENSE inside that installed folder. Use Windows Blender 5.3.0 for native liquid and gas. For the standalone Geometry Nodes CLI, set `BLENDER_PATH` to a supported Blender executable.
