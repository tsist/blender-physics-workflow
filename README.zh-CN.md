# Blender 物理工作流 — 0.8.1-dev.1

[English](README.md) · [下载](https://github.com/tsist/blender-physics-workflow/releases/tag/v0.8.1-dev.1) · [接口契约](docs/CONTRACT.md)

可编辑的几何节点物理工作台、供 AI 助手使用的配套技能，以及可选 CLI。已验环境为 **Blender 5.2.1 LTS / 9e2066aef7ef**。本版以 **GPL-3.0-or-later** 开源，标记为开发预发布版。

## 功能与适用范围

- 球代理刚体、布料与绳索、粒子发射及表面重建、弹性表面壳、平面箱体倾倒。
- 可编辑节点、源对象保护、手改保留、磁盘烘焙、检查，以及静态网格、Alembic、表面 OpenVDB 导出。
- CLI 参数扫描、批处理、状态查询、取消和基于完整输出校验的候选恢复。
- 检查面板区分网格离地距离与实例原点；缺少运动证据时明确显示未知，并读取真实休眠状态。

已验代表场景包括200球容器、2000球稀疏负载、10000顶点布料和52520实际粒子。它们不代表任意场景的性能或质量保证，详见[阶段验证](docs/STAGE-VALIDATION.md)和[诊断验证](docs/DIAGNOSTICS-VALIDATION.md)。

粒子系统未实现压力或不可压缩流体；弹性壳不是实体体积软体。箱体倾倒只支持XZ平移、Y旋转和固定水平地面，不含箱体间碰撞。Bullet、Mantaflow桥接仍为预留接口。

## 安装 Blender 插件

1. 在发布页下载 `physics_nodes_workflow_addon-0.8.1-dev.1.zip`。
2. 打开 Blender 的**编辑 → 偏好设置 → 获取扩展 → 菜单 → 从磁盘安装**，选择该ZIP；不要选源码包或技能包。
3. 启用 **Physics Nodes Workbench**。选择目标对象，按 `N` 打开3D视图侧栏的 **Physics Nodes** 页签。
4. 选择预设后执行 **Build / Update**。默认保护源对象并创建工作副本；随后 **Bake to Disk → Inspect Simulation**，保存工程。

保留工程所需的受管缓存目录。旧图升级可能需要在工作副本上明确 **Rebuild**，旧图会保留；从v0.1.0迁移前先备份工程。安装新版本身不会重建旧图，也不会使旧缓存自动有效。

## 安装配套技能

下载 `blender-physics-workflow-skill-0.8.1-dev.1.zip`，将其中的 `blender-physics-workflow` 文件夹放到助手的技能目录。Codex通常使用 `$CODEX_HOME/skills` 或 `~/.codex/skills`。也可直接查看仓库中的[技能正文](skills/blender-physics-workflow/SKILL.md)。

技能ZIP只包含操作说明，不包含可执行工具。还需克隆本仓库或解压 `blender-physics-workflow-0.8.1-dev.1-source.zip`，并告诉助手该源码目录。技能中的 `<REPO_ROOT>` 指这个目录，`docs/` 路径也相对于该目录。技能文件夹内附有许可证；技能不会自动安装Blender或插件。

## 可选 CLI

插件与当前CLI均可独立运行，不需要另外获取blenderctl仓库。使用Python 3.12或3.13，并以 `BLENDER_PATH` 指定已验Blender程序，在源码目录执行以下命令。

```powershell
$env:BLENDER_PATH = 'C:/path/to/blender.exe'
python -B tools/physics_workflow_cli_v1/cli.py describe
python -B tools/physics_workflow_cli_v1/cli.py run --request request.json --jobs-dir ./jobs
python -B tools/physics_workflow_cli_v1/cli.py resume --manifest C:/jobs/run-id/resume_plan.json
```

请求示例见[技能正文](skills/blender-physics-workflow/SKILL.md)，操作范围见[契约](docs/CONTRACT.md)。取消以原执行进程确认终态为准；恢复会校验已完成候选，并从原始输入重放未完成候选，不是任意中间帧续算。

## 验证与源码构建

```powershell
python -B scripts/verify_release.py
python -B -m unittest discover -s tests -p "test_*.py" -v
python -B scripts/build_release.py --output ./dist
```

发布附件包括插件包、源码/CLI包、技能包和 `SHA256SUMS`，三个ZIP均携带许可证。云端CI在Windows/Linux检查源码、契约与可复现打包；缺少指定本机Blender环境时，依赖Blender的测试会跳过。实际模拟和界面验证的范围见[验证文档](docs/VALIDATION.md)。
