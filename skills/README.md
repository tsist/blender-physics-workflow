# Companion Agent Skill

This repository ships the portable agent skill for Physics Nodes Workflow:

- `blender-physics-workflow`: [SKILL.md](blender-physics-workflow/SKILL.md)

The skill is an instruction package, not an executable bundle. Keep a checkout of this repository or extract the matching source ZIP, then tell your assistant its absolute location. `<REPO_ROOT>` means this source directory; `docs/` references are relative to it.

For the standalone skill ZIP, copy its `blender-physics-workflow` folder (including LICENSE) into your agent skills directory. When installing from the repository, copy `skills/blender-physics-workflow` and place a copy of the repository LICENSE inside that installed folder. Set `BLENDER_PATH` to Blender 5.2.1 LTS. A separate blenderctl checkout is not required by the current CLI.
