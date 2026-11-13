# Blender Physics Workflow（模块化物理节点工作流）

[English](README.md) · [契约说明](docs/CONTRACT.md) · [验证与边界](docs/VALIDATION.md)

面向 **Blender 5.2.1 LTS**（已验证构建 `9e2066aef7ef`）的模块化 **Geometry Nodes** 物理工作台插件（**0.1.0**）、隔离配套 CLI（**0.1.0**）与可移植 Agent 技能（**0.1.0**），采用 **GPL-3.0-or-later** 许可协议。

配套 CLI 集成 PR 目标仓库：[tsist/blenderctl](https://github.com/tsist/blenderctl)。

---

## 核心架构

1. **Wrapper 层（`PNW_Wrapper_<obj>_<mod>`）**：面向修改器的外层接口节点组，在参数更新、重构建与磁盘烘焙期间保留用户在节点编辑器中插入的节点（如 `User_Added_*`）与自定义接口。
2. **Core 层（`PNW_Core_<obj>_<mod>`）**：由 **1 个根级 `GeometryNodeSimulationInput/Output`** + 尾部 `GeometryNodeBake` + 内部 `GeometryNodeRepeatInput/Output`（`substeps = 1..8`, `dt_sub = Delta Time / Substeps`）构成，保证单个修改器与单个 `DISK` 缓存目录 1:1 绑定。
3. **12 个无状态 `PN_*` 可编辑子组**：
   - 力场：`PN_Force_GravityDrag`、`PN_Force_WindTurbulence`
   - 碰撞：`PN_Collide_GroundPlane`、`PN_Collide_MeshSurface`
   - 约束：`PN_Constraint_PinAnchor`、`PN_Constraint_DistanceChain`
   - 步进求解器：`PN_Solver_RigidScatterStep`、`PN_Solver_ClothRopeStep`（基于相对静置位移 `p_pred - pnw_rest_pos` 的 PBD 模糊与速度耦合）、`PN_Solver_FluidParticleStep`
   - 后处理：`PN_Post_InstanceRigid`、`PN_Post_FluidSDFMesh`（`PointsToSDFGrid -> GridToMesh`）、`PN_Post_SmokeVolume`（`PointsToVolume -> VolumeToMesh`）

---

## 支持的物理域、预设与桥接模式

- **合约标识**：`BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1`（`schema_version: 1.0`）
- **三域与 6 套预设**：
  - `rigid_scatter`：`rigid_drop_pack`、`rigid_chain_pendulum`
  - `cloth_rope`：`cloth_banner_wind`、`rope_catenary_swing`
  - `fluid_emission`：`fluid_fountain_sdf`、`smoke_plume_volume`
- **支持操作**：`build`、`update`、`parameters`、`bake`、`invalidate_cache`、`inspect`、`adopt`、`export`
- **桥接模式**：`GN_NATIVE`（默认）、`BULLET_BRIDGE`、`XPBD_ASSET_BRIDGE`、`MANTAFLOW_BRIDGE`
- **导出格式**：`NONE`、`STATIC_MESH`、`ALEMBIC`、`.vdb` (`OPENVDB`)
- **N-Panel 面板**：`3D Viewport -> N-Panel -> Physics Nodes`（`VIEW_3D_PT_physics_nodes_workbench`）

---

## 快速开始

### 1. 主机端单元测试与契约查询（需 Python 3.12+）

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
python -B tools/physics_workflow_cli_v1/cli.py describe
```

### 2. 通过 CLI 运行物理仿真（需安装 Blender 5.2.1 LTS）

```powershell
$env:BLENDER_PATH = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
python -B tools/physics_workflow_cli_v1/cli.py run `
  --request examples/fluid_fountain_sdf.json `
  --jobs-dir ./runtime/jobs `
  --timeout-seconds 300
```

CLI 子命令包括：`describe`、`run`、`sweep`、`batch`。

### 3. 安装插件与技能

- **Blender 插件**：从 [Releases](https://github.com/tsist/blender-physics-workflow/releases) 下载 `physics_nodes_workflow_addon-0.1.0.zip`，在 Blender **Preferences -> Extensions -> Install from Disk** 中安装并启用。
- **Agent 技能**：下载 `blender-physics-workflow-skill-0.1.0.zip` 或复制 `skills/blender-physics-workflow/` 到你的 Agent 技能目录。

---

## 验证范围声明

Windows + Blender 5.2.1 LTS 本机验证覆盖：契约单元测试、`blender --command extension validate`、三域 `GN_NATIVE` 构建/磁盘烘焙/冷重开核验、多帧 EEVEE 渲染检查及 Alembic（`.abc`）导出。GitHub Actions 主机 CI 仅覆盖静态清单哈希、Python 语法、单元测试、`cli.py describe` 与可重复 ZIP 构建，不包含 Linux 下 Blender 运行时验证。详见 [验证与边界](docs/VALIDATION.md)。
