# 接口、源码与验证资料

REPO_ROOT 指本仓库或解压后的源码目录。以下文档链接可从 GitHub 直接打开。

- 几何节点独立 CLI 从 REPO_ROOT 运行 `python -B tools/physics_workflow_cli_v1/cli.py describe` 查询预设、作业和续跑契约。其契约 ID 为 BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1，schema_version 为 1.0。[接口文档](https://github.com/tsist/blender-physics-workflow/blob/main/docs/CONTRACT.md)
- 插件源码位于 REPO_ROOT/tools/physics_nodes_workflow_addon/。Blender 5.3 原生流体面板的域、交接和密度清单见[实操流程](native-fluid.md)。
- 几何节点输出使用 physics-geometry-evidence-v2。交换文件旁车清单为 pnw.export.delivery/1。受管续跑清单为 pnw.workflow.resume_plan，version 1。[验证范围](https://github.com/tsist/blender-physics-workflow/blob/main/docs/VALIDATION.md) · [历史量级验证](https://github.com/tsist/blender-physics-workflow/blob/main/docs/STAGE-VALIDATION.md)
- Blender 原生域设置和 5.3 几何节点的官方资料。[流体域](https://docs.blender.org/manual/en/5.3/physics/fluid/type/domain/index.html) · [Blender 5.3 几何节点](https://developer.blender.org/docs/release_notes/5.3/geometry_nodes/)
