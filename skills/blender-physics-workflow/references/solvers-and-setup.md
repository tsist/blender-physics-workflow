# 求解器、参数边界、固定点与面板交接

本页汇总各物理域的适用范围、核心参数边界、固定点与面板交接细则，配合 [SKILL.md](../SKILL.md) 查阅。

## 1. 求解域与预设速查

本页的刚体、布料和粒子预设使用 `GN_NATIVE`。旧几何节点面板中的 `BULLET_BRIDGE`、`XPBD_ASSET_BRIDGE`、`MANTAFLOW_BRIDGE` 选项仍属于旧契约的预留接口。Blender 5.3 的原生域从 `native_liquid` 或 `native_gas` 预设和 **Native Fluid** 面板进入。[原生流体设置](native-fluid.md)

| 求解域 | 预设 ID | 适用行为 | 使用要点 |
| --- | --- | --- | --- |
| `rigid_scatter` | `rigid_drop_pack` | 刚体散落与堆叠 | 按 `particle_radius` 做球代理碰撞；无面网格在 `scatter_count: 0` 时逐顶点生成球代理；输出实例原点在球心 |
| `rigid_scatter` | `rigid_chain_pendulum` | 链条悬挂摆动 | 链点按顶点索引相邻串联，首点到 `anchor_pos` 保持 `rest_length`；散点把 `rest_length` 设为 `0` |
| `rigid_scatter` | `rigid_box_tipping` | 单位质量箱体倾倒 | 在 XZ 平面平移、绕 Y 轴转动并与水平地面碰撞；每顶点一个箱体（最多 64 个），真实尺寸为 `2 * box_half_extents`，初始倾角为 `box_initial_tilt`；只接受 X/Z 重力，关闭目标上游修改器；单点触地后会有轻微侧躺摇摆 |
| `cloth_rope` | `cloth_banner_wind` | 旗帜与布片迎风 | 接受有面网格；可开启 `bending`（`bend_softness: 0..1`）和 `tearing`（`tear_threshold: 1..100`）；依赖 `pnw_pinned` 固定顶点 |
| `cloth_rope` | `rope_catenary_swing` | 悬链与绳索摆动 | 曲线按 `curve_resolution` 重采样，保留固定点属性并在重采样后复查固定效果 |
| `cloth_rope` | `softbody_elastic_shell` | 闭合弹性软壳 | 要求本地闭合三角流形且体积非零、关闭上游视口与渲染修改器（物理后的后处理可保留）；默认开启 `bending`，内部把 `Softness` 映射为 `1 - Stiffness` |
| `fluid_emission` | `fluid_fountain_sdf` | 粒子喷射与表面包裹 | 做粒子运动与 SDF 表面重建预览；真实粒子总数写在重建表面的 `INT/POINT` 属性 `pnw_particle_count` 上 |
| `fluid_emission` | `smoke_plume_volume` | 烟羽浮力与体积预览 | 浮力作为世界 `+Z` 加速度叠加在重力上，粒子寿命按帧数随场景帧率走 |

## 2. 世界坐标与核心参数边界

求解核心先把输入几何按完整对象矩阵变到世界坐标，算完再用逆矩阵转回。重力、风场、`anchor_pos`、`ground_z`、半径和厚度都按世界坐标填写。提前排查奇异矩阵、目标自引用碰撞集合与类型不符的碰撞体，确保在进入求解前就清理干净。

关键参数范围（数值须有限，整数参数填整数）：

- `substeps`：`1–8`
- `curve_resolution`：`2–50000`（默认 `64`）
- `contact_iterations`：`1–32`（默认 `3`，控制球代理接触迭代）
- `constraint_iterations`：`1–64`（默认 `12`，控制布料与绳索边长迭代）
- `activation_frame`：`0–100000`（默认 `0`）

常用调节经验：

- **实例原型归一化**：`instance_normalize: true`（默认）逐个原型按包围盒中心与半对角线缩放进半径包围球，保持造型比例；设为 `false` 时按 `2 * particle_radius` 倍原尺寸缩放。自定义非球原型在判断间隙时，把球代理碰撞面和可见网格外形分开核对。
- **高面数布料**：万级顶点布料把 `substeps` 提到 `8`、`constraint_iterations` 提到 `64` 再试跑，避免边长拉长或抖动。
- **刚体整组按帧激活**：`rigid_scatter` 域把 `activation_frame` 设为正整数场景帧时，目标帧之前原生 Simulation Skip 会保持位置、旋转和零初速；到达触发帧后开始正常受力。起始帧固定做初始化，触发帧早于或等于起始帧时从下一帧跑第一个时间步，晚于结束帧则全程保持静置。输出 `POINT` 到 `INSTANCE` 布尔属性 `pnw_active` 供检查。等待中的组若要参与别的物理碰撞，按普通碰撞体显式指定。

## 3. 碰撞体与源对象保护

- **源对象保护**：界面首次 Build 默认复制对象和网格到独立工作副本，后续 Build 复用；CLI 始终读取不可变输入 `.blend` 并写出全新的输出 `.blend`，保护原文件与共享数据块。
- **保留作者手改**：Wrapper、Core 与嵌套节点组靠真实数据块引用识别。普通参数更新只改修改器输入，原样保留 Core、Wrapper、新增节点和主动断开的连线；遇到共享节点图会自动复制隔离。
- **切换域或重建**：跨预设切换求解域时，先检查图里有没有手改节点，再在工作副本上跑 `build` + `force_rebuild: true`，新建替代图并保留旧 Wrapper。切到 `rigid_box_tipping` 时，界面会自动忽略其它预设留下的碰撞体、实例集合、散点或 Y 重力，原值留着切回时用；CLI 请求只填当前预设支持的参数。
- **碰撞体动画**：单体和集合里的普通网格碰撞体支持 `location`、`rotation_euler`、`rotation_quaternion`、`scale` 关键帧动画。签名覆盖真实绑定的 Action slot/channelbag、曲线控制点、手柄、插值与静态父级，保持帧与选区干净。NLA、驱动器、约束、形态键或目标自身动画先烘焙成普通关键帧网格再接入。

## 4. 固定点操作（`physics pin-selection`）

布料和绳索只在有显式 `POINT/FLOAT` 属性 `pnw_pinned` 时固定顶点，固定点以原生质量 `0` 参与约束，其余点保留已有质量（缺省为 `1`）。无固定点时布料按自由落体计算。

查看命令行参数：

```powershell
& './blenderctl.ps1' physics pin-selection --help
```

JSON 参数字段包含：`file`、`expected_sha256`、`scene`、`view_layer`、`object`、`operation`。

- 只读查询现有固定点：`operation` 填 `inspect`，省略 `vertices` 和 `--output`。
- 写入或解除固定点：`operation` 填 `pin` 或 `unpin`，在 `vertices` 里给出去重后的零基顶点索引列表，并指定全新的 `--output` `.blend` 路径。物体模式会自动隔离共享网格数据，编辑模式直接写入单用户活动网格。

## 5. 面板工作副本交接（`physics panel-copy`）

需要在 Blender 界面里手调参数、点 **Bake to Disk** 或把草稿交回 CLI 时，用 `physics panel-copy` 生成带面板身份的候选 `.blend`：

```powershell
& './blenderctl.ps1' physics panel-copy --help
```

常用 `operation`：

- `inspect`：只读查看当前保存的面板草稿。
- `build`：遵循原生 `protect_source` 规则，复用已标记的工作对象。
- `copy`：新建一份原生工作副本。
- `reset-preset`：重置预设草稿参数（可选传 `preset_id`），等下次构建再生效。
- `set-bridge-mode`：只保存求解器下拉项（可用 `GN_NATIVE`）。

`build` 和 `copy` 可带入 `preset_id`、`modifier_name`、`protect_source`、`bridge_mode`、`collider_object`、`collider_collection`、`instance_collection` 与 `parameters`，写入操作指定新 `--output` `.blend`。界面点 **Bake to Disk** 会先应用当前面板参数再烘焙；如果作者在节点图里手动断开了 Core 输入，复查时直接看节点图和缓存身份，以实际求值输入为准。
