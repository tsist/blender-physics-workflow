# 求解器、参数边界、固定点与面板交接

本页汇总各物理域的适用范围、核心参数边界、固定点与面板交接细则，配合 [SKILL.md](../SKILL.md) 查阅。

## 1. 求解域与预设速查

下表列出几何节点预设，独立 CLI 通过 `GN_NATIVE` 执行。Blender 5.3 液体和气体域另见[原生流体面板](native-fluid.md)。

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

## 4. 固定点和面板交接

布料和绳索用显式 POINT/FLOAT 属性 pnw_pinned 固定顶点。局部工作对象的网格应为单用户。选中顶点，在 **Physics Nodes** 面板使用 **Pin Selected** 或 **Unpin Selected**，存盘后让独立 CLI 从该工作副本读取属性。曲线需在重采样后复查实际固定点。

接手面板时，先另存工程并打开 **Build on Working Copy**。普通 **Build / Update** 复用工作对象并保留作者节点编辑。界面 **Bake to Disk** 先应用当前面板设置再烘焙。如果作者手动断开 Core 输入，检查实际节点连线与缓存身份。切换预设前留一个保存点，必要时显式 **Rebuild**。
