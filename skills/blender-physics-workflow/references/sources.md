# 契约入口、源码位置与阶段实证索引

本文件归档物理节点工作流的公共契约、源码目录、历史阶段审计路径与验收范围记录，正文操作流程见 [SKILL.md](../SKILL.md)。

## 公共契约与源码入口

- **公共启动器**：`./blenderctl.ps1`（通过 `discover --product physics` 与 `physics describe` 实时查询当前宿主版本、插件版本、预设列表与参数边界）
- **公共契约**：`BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1`（`schema_version: 1.0`）
- **几何与属性证据**：`physics-geometry-evidence-v2`
- **交付清单与续跑清单**：`pnw.export.delivery/1`、`pnw.workflow.resume_plan`（version `1`）
- **工作区插件源码**：`<PROJECT_ROOT>\tools\physics_nodes_workflow_addon\`
- **工作区独立 CLI 源码**：`<PROJECT_ROOT>\tools\physics_workflow_cli_v1\cli.py`
- **公共发布版 CLI 路径**：以 `blenderctl.ps1 discover --product physics` 返回的 `implementation_paths` 为准
- **5.3 原生流体**：Windows Blender `5.3.0` / `02f0899d78e3`，工作区插件 `0.9.0-dev.2`，入口动作 `pnw.native_fluid`，密度清单 `pnw.native-density-sequence/1`。[插件阶段证据](<work/audit/<batch>/>) · [公共 CLI 实跑](<work/audit/<batch>/>)
- **CLI 交接文档**：`<PROJECT_ROOT>\docs\cli\workflows\physics-and-editing.md`
- **动作范围参考**：`work/audit/<batch>/`

## 阶段实证与历史审计路径

- **公共入口固定点回执**：`work/audit/<batch>/`
- **倾倒与缓存实证**：`work/audit/<batch>/`
- **阶段验收总矩阵与各域报告**：`work/audit/<batch>/`（同目录含 `STAGE-REPORT.md`、`TIPPING-ACCEPTANCE.md`、`SOFTBODY-ACCEPTANCE.md`、`package-check.json`）
- **诊断增量实证**：`work/audit/<batch>/`
- **已归档工作记忆**：`wm-<date>-<topic>`、`wm-<date>-<topic>`

## 代表规模实证与待补齐能力

- **已验证代表规模**：200 球容器/坡道/帧 12 触发 250 帧静置与冷重开、2000 球稀疏输入 250 帧、10000 顶点旗布 120 帧（8 子步 × 64 约束迭代）、42 顶点弹性壳 120 帧局部形变恢复、52520 粒子 250 帧与第 4 帧闭合网格表面 OpenVDB 读回、箱体倾倒临界角对照 120 帧及可见面板修改重烘冷重开。
- **旧几何节点路线的边界**：布料剪切与自碰撞、完整实心柔体体积与内部压力、任意复杂接触岛普遍静置、中间帧断点续算及跨主机锁尚无通用验收。真实容器填充与固定气体域 `density` VDB 已按 [5.3 原生流体路线](native-fluid.md) 实跑。容器闭合表面体积是几何量测，已交付的烟气序列为 `density`。
