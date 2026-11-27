# 缓存校验、诊断口径、导出格式与断点续跑

本页用于 `GN_NATIVE` 的磁盘缓存、几何诊断、网格导出及长作业控制。Mantaflow 域的独立冷开、烟气密度 VDB 与缓存恢复见[原生流体工作流](native-fluid.md)。

## 1. 受管磁盘缓存与冷重开验证

- **独立代次目录**：每次烘焙写入全新的受管代次目录，旧代缓存和无关文件保持原样。把 `--jobs-dir` 设在较短路径下，避免触碰 Windows 259 字符缓存文件路径上限。
- **签名覆盖范围**：缓存指纹覆盖真实修改器值、递归节点语义、几何属性、变换、碰撞体与实例集合依赖、场景时基及文件完整性。只拖动节点排布位置保持缓存有效；改动节点运算模式、默认值、ColorRamp、CurveMapping、触发帧或有效材质槽后，旧缓存自动失效，重新烘焙出新代。
- **读写所有权分离**：另存工程可以只读消费旧代缓存，发起新烘焙时建立新的所有权代次。遇到无法完整签名的外部依赖时，排查补齐后再签收。
- **独立冷重开**：开启 `verify_cold_reopen: true` 后，系统会另起干净的 Blender 进程重开输出工程，先验缓存清单和文件完整性，再按 `physics-geometry-evidence-v2` 核对顶层 `Mesh`、`PointCloud`、`Curves`、`INSTANCE` 的几何摘要、实例原型对应关系与全部 `pnw_` 属性（数值按六位量化比对）。读取实例属性时直接从实例层取值，保留真实实例级属性。

## 2. 诊断报告关键字段怎么看

跑完 `inspect` 或带烘焙的 `run` 后，按下面口径读检查报告，再结合渲染图判断效果：

- **`frames[帧号].ground_contact`**：把 `instance_origins`（实例原点高度）和 `visible_geometry`（求值网格及网格实例全部世界顶点到 `ground_z` 的有符号最小距离）分开报告。负数表示顶点落到地面下方，穿透顶点数按 `1e-4` 容差统计。旧字段 `ground_clearance_min` / `ground_penetration_count` 按 `ground_measurement_scope` 兼容保留，新脚本优先读 `ground_contact`。
- **`frames[帧号].solver_sleep` 与 `resting_ratio`**：`resting_ratio`（界面称 Low-speed fraction）统计采样速度低于阈值的比例；真实求解器休眠由 `solver_sleep` 下的 `pnw_sleep` 独立报告，配合 `pnw_active` 和 `pnw_sleep_rooted` 判断激活与支撑状态。刚体休眠在向下重力且有有效支撑时生效，环境、支撑根数或法线变化会自动唤醒。
- **速度与缺失处理**：报告顶层提供 `mean_speed`、`max_speed` 和 `speed_source`（原始速度统计位于 `geometry_summary.physics_attributes.velocity_statistics`）。无输出、前后帧点数变化或缺少速度属性时，速度和低速比例返回 `null`、`speed_source` 标为 `not_observable`，具体原因查 `late_motion`。粒子表面重建的 `pnw_particle_count` 记录真实模拟粒子总数，空表面或缺失属性记为不可观察。

## 3. 原型材质准备与三种磁盘导出

刚体实例原型如果用了 OBJECT 级材质覆盖，直接 Realize 会丢失覆盖。先在请求里跑 `operation: "prepare_instances"`（界面对应 **Prepare Prototype Copies**）：

- 指定本地、静态、非空且无子集合的网格集合 `instance_collection`。
- 系统按 Blender 原生 Collection Info 实例行序逐个复制出独立网格，把有效材质槽固化到 DATA 层，保留 UV、顶点组与变换，材质数据块保持共享。
- 准备完成后，从回执 `prepared_instances.prepared_collection` 取出新集合名，填进后续 `build` 或 `run` 请求里再做烘焙与导出。

三种磁盘导出都会生成主文件与旁车清单 `<主文件>.pnw-export.json`（schema `pnw.export.delivery/1`），记录主文件、外部依赖的 SHA256、文件大小与保留/舍弃项说明。写出时始终给新路径，保护已有导出文件：

| 导出格式 | 产出内容 | 保留项 | 交接注意 |
| --- | --- | --- | --- |
| `STATIC_MESH` | 独立静态对象 `.blend` 副本 | 固化目标有效材质槽、多套 UV 及活动/渲染选择、逐面材质、着色器图、外部与打包贴图清单（相对路径自动重映射） | 先把目标贴图存盘并转为普通本地单图（避开外链、缺图、UDIM 或序列），打包搬运时一并带齐外部贴图 |
| `ALEMBIC` | 实现后的单一动态网格序列 `.abc` | 多套 UV（Blender 扩展）与逐面材质槽名（`face_sets`） | 下游重导入后按材质槽名重新挂接着色器与贴图，并按需指定活动渲染 UV；先确认有效缓存且至少有一帧非空几何 |
| `OPENVDB` | 表面 level-set 体素 `.vdb` | 闭合水密世界网格的体素、变换与正负符号，写出后自动独立读回核验 | 用于表面 level-set 交换；需要烟气密度时用[原生气体与密度 VDB](native-fluid.md) |

## 4. 作业运行、扫参、批处理与断点续跑

在源码目录以已配置的 BLENDER_PATH 运行独立 CLI。可加 `--help` 查参数。

```powershell
$cli = './tools/physics_workflow_cli_v1/cli.py'

# 单次运行 / 扫参 / 批处理
python -B $cli run   --request $reqJson --jobs-dir $jobs --timeout-seconds 300
python -B $cli sweep --request $sweepJson --jobs-dir $jobs --timeout-seconds 300
python -B $cli batch --request $batchJson --jobs-dir $jobs --timeout-seconds 300

# 查状态 / 协作取消 / 按清单续跑
python -B $cli status --manifest $manifest
python -B $cli cancel --manifest $manifest
python -B $cli resume --manifest $manifest --timeout-seconds 300
```

使用要点：

- **回执与锁机制**：`run`、`sweep`、`batch` 都会写出持久 `resume_plan.json`（schema `pnw.workflow.resume_plan` version `1`）。原任务与 `resume` 共用同机 OS 文件锁，并发竞争时返回 `RESUME_LOCKED`，进程结束后自动释放。
- **按候选精确重放**：`resume` 先逐项核对输入 SHA、请求、回执、输出、导出主文件与旁车清单、缓存清单与全部缓存文件以及冷开证据；全部齐全且一致的候选直接跳过，失败、挂起或损坏的候选从原输入重新跑到新作业目录，保留旧输出供对照。更改输入文件或归一化参数后新建一轮 `run` / `sweep` 即可。
- **协作取消**：`cancel` 提交取消请求后，由原执行进程树自行收尾、释放锁并写入 `cancelled` 终态；新的 `resume` 使用新控制代次，旧取消请求互不影响。
- **排查失败**：每次运行完同时检查退出码、返回 JSON 里的结构化 `error` 字段以及 `--jobs-dir` 下的磁盘日志。
