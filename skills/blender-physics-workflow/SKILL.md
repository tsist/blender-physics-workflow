---
name: blender-physics-workflow
description: 在 Blender 5.2.1 中使用物理节点工作台和独立 CLI 制作、检查及烘焙刚体代理、布料绳索、粒子发射；按真实支持范围选择求解器，保护源对象与手改，校验缓存和冷开输出。
---

# 物理节点工作流

安装前提：本技能包仅包含说明。另需本仓库的匹配版本源码（git checkout或source ZIP）及Blender 5.2.1，并由用户提供源码目录。下文 `<REPO_ROOT>` 为该源码目录，不是技能安装目录；`docs/` 路径均相对于源码目录。先检查源码中的插件/CLI路径存在，再执行；未找到时说明需要匹配源码，不自动下载或安装。当前CLI独立运行，不要求另一个blenderctl仓库。

本地源码是 **0.8.1-dev.2 开发中版本**，已验证 Windows Blender 5.3.0（`02f0899d78e3`）和 5.2.1 LTS（`9e2066aef7ef`）。冻结需求仍保留；候选代码、单个通过用例与整个产品验收是三种不同状态。工作流可用性与求解质量分开验收，局部后端或算法限制不阻塞独立接口、缓存、导出及恢复开发。

当前能力和验证边界见 docs/STAGE-VALIDATION.md 与 docs/DIAGNOSTICS-VALIDATION.md。不得从节点存在或烘焙成功推定支持。

## 选择制作路线

先明确输入几何、接触形状、固定点、时间范围和用户要看到的行为。球代理堆叠、相邻链约束、布面保边长、弹道粒子和不可压缩液体的验收不同。先选最小能区分原因的场景，再扩展至冻结规模。

- 核心入口：`<REPO_ROOT>/tools/physics_nodes_workflow_addon/`。
- 独立 CLI：`<REPO_ROOT>/tools/physics_workflow_cli_v1/cli.py`，先 `describe` 核对当前接口。
- 公共 contract：`BLENDER_PHYSICS_WORKFLOW_CONTRACT_V1`，`schema_version: 1.0`。
- 主路径为 `GN_NATIVE`。Bullet、Mantaflow 和旧 XPBD bridge 标签是预留接口，未完成适配时返回 `UNSUPPORTED_BRIDGE`；不得堆叠两个求解器冒充桥接完成。
- OpenVDB 已实现闭合世界网格的真实 surface level-set 写出与独立读取校验；不等于原生烟雾 density/velocity grid 导出。

不修改无关的 `tools/blenderctl/`、几何节点、UV 或绑定工作流；不全局安装、切换用户 Blender 环境或远端发布。

## 保护输入与作者编辑

界面首次 Build 默认复制对象和几何数据到独立工作副本，后续 Build 复用；已有明确工作对象可关闭 Protect Source。CLI 从不可变输入 `.blend` 生成新输出，不得以 input=output 或 hardlink 别名覆盖源文件。

Wrapper、Core 和嵌套组按真实 datablock 引用归属工作流。名称只是显示标签。普通更新只写 Modifier 输入，保留 Core、Wrapper、用户新增节点和主动断开的连线。共享图在更新时复制隔离。

预设/域变化不能伪装成参数更新。旧版图或需要换求解结构时，先检查作者修改，再在工作副本上显式 `build` + `force_rebuild: true`；新建替代图并保留旧 Wrapper，不原地清空旧节点。报告保留图名并验证非目标对象。

## 坐标、参数与固定点

核心把输入几何用完整对象矩阵变换至世界坐标，在世界坐标中求解，最后用逆矩阵还原。重力、风、Anchor、Ground Z、半径和厚度按世界坐标解释；不得仅减平移。单体碰撞体和集合的读取方式不同，应核对实际求值。奇异矩阵、目标自引用集合、错误碰撞体类型应在边界拒绝。

`substeps` 范围 1–8；`curve_resolution` 控制曲线重采样（默认 64，范围 2–50000）；`contact_iterations` 控制球代理接触迭代（默认 3，范围 1–32）；`constraint_iterations` 控制布料/绳索边长迭代（默认 12，范围 1–64）。寿命使用帧数并跟随 FPS / FPS Base；浮力作为世界 +Z 加速度叠加重力。数值必须有限，整数参数不接受小数、NaN 或 Infinity。

刚体接触仍按 `particle_radius` 球代理处理。`instance_normalize: true` 默认逐原型按 bbox 中心和半对角线保形归一化，缩放至半径包围球；False 保留旧 2r 倍原始尺寸。几何/材料保留，内部嵌套实例身份与实例级属性不保留，非球可见形状不等于球接触面。三域均支持单体和集合碰撞，单体回退使用世界几何。链点索引为相邻顺序，首点到 Anchor 同样为 `rest_length`；散点用0，不代表任意分支或铰链。

0.8另有 `rigid_box_tipping`：单位质量箱体的XZ平面运动、Y轴角动力学与固定水平地面接触。原始Mesh每个顶点一个箱体，最多64个；真实尺寸为 `2*box_half_extents`，倾角为 `box_initial_tilt`。法向/切向冲量产生真实角速度；不使用球代理或线速度spin代签倾倒。仅支持Gravity X/Z，无箱间碰撞或任意碰撞体；禁止上游启用的修改器绕过输入上限。单点接触仍有侧躺蠕动/摇摆，不签完全静置。临界角上下对照、120帧bake/cold和可见UI已验，详见docs/STAGE-VALIDATION.md。

切到箱体预设时，界面请求忽略其它预设留下的bridge/碰撞体/实例集合/scatter/Y重力值，原设置保留供切回；CLI不接受不受支持的值。参数修改后用Bake会应用新值并生成新缓存，作者主动断开的连线仍保持。可见界面 Build→Bake→Inspect→修改半尺寸→Bake→Inspect→保存→独立冷开已验；这不替代每个新增求解器的物理验收。

布料新增 `bending`/`tearing`（默认false），分别使用原生 Cross Edge 约束与 POST_SOLVE 撕裂效应；仅有面 Mesh，非 Mesh 拒绝。`bend_softness: 0..1`、`tear_threshold: 1..100`，均进入参数身份。无自碰撞或完整柔体资格。

0.8新增 `softbody_elastic_shell`，限定本地闭合三角流形表面弹性壳，默认开启bending。创建ID前拒绝开放、松散、非流形、非零体积条件不满足，以及视口或渲染启用的上游Modifier；物理Modifier之后的后处理允许。内部Elastic Shell分支使用原生Softness=1-Stiffness，旧cloth/rope映射保持不变。42顶点壳120帧局部形变恢复/源保护/冷开已验，详见docs/STAGE-VALIDATION.md；不是实体体积/压力/自碰撞求解。Stiffness控制edge应变，不承诺宏观形状单调；关闭bending的负控制会塌陷，不签为可用软壳配置。

布料使用显式 POINT/FLOAT 属性 `pnw_pinned`，没有该属性时不自动冻结顶端带。0.8使固定点以原生质量0参加约束，非固定点保留已有mass，缺省mass=1；边长容差随最短rest edge缩放。界面 Pin Selected / Unpin Selected 用于选中的网格顶点。曲线可携带固定点属性，并在重采样后检查结果；不能把没有固定点的自由落体当作悬挂失败。10000顶点旗布/120帧已验8子步×64约束迭代，低迭代失败证据保留；不能据此承诺所有网格使用默认迭代即达相同质量。

UI **Bake to Disk** 会先应用当前工作台参数，再按实际求解输入烘焙。作者断开 Core 输入后，Modifier 值可能不再控制对应物理量：需检查图和实际缓存身份，不能单看 UI 值。

刚体整组按帧激活用 `parameters.activation_frame` / UI **Activation Frame**：0立即参与；正整数为绝对场景帧，范围0–100000。到帧前原生Simulation Skip保持位置、旋转和零初速，同时不执行碰撞修正或链约束；到帧才开始正常模拟。起始帧始终是初始化，因此触发帧早于/等于起始帧时从下一帧执行第一个时间步；晚于结束帧允许全程等待。仅rigid域支持非0，其它域明确拒绝。输出POINT→INSTANCE布尔属性 `pnw_active` 表示该组是否已激活，已纳入v2属性冷开验证。

此功能为整组时间触发，不是逐点唤醒、碰撞触发或跨Modifier碰撞系统。等待的组不是其它独立工作流的自动静态碰撞体，需按原支持范围显式指定碰撞输入。可以用CLI sweep比较触发时机；每个候选独立烘焙/冷开，不能将不同候选当同时耦合的场景。触发参数或实际作者默认值变化使缓存失效。旧0.6图必须明确Rebuild以加入新结构并保留旧图；普通更新不得只签新参数却仍运行旧图。测试覆盖整数帧回放，任意子帧/运动模糊触发边界未验。

## 缓存、批处理与导出

`rigid_drop_pack` 的无面 Mesh 输入在 `scatter_count: 0` 时逐顶点生成球代理；半径由 `particle_radius` 决定。输出实例的原点是球心，不能把其高度当成球表面净空。

0.8.1 检查输出的 `frames[帧号].ground_contact` 明确区分 `instance_origins` 与 `visible_geometry`：后者测量求值 Mesh 及 Mesh 实例的全部世界坐标顶点到 `ground_z` 水平面的有符号最小距离，包含旋转、缩放、嵌套与混合网格。负数表示顶点在平面下方；穿过平面的顶点数采用1e-4容差，不能当成穿透物体数。它不测任意碰撞体距离，也不代表球代理碰撞面的净空；自定义非球原型尤其需分开判断。非Mesh组件会报告partial或not_observable。旧 `ground_clearance_min` / `ground_penetration_count` 保留兼容，其口径见 `ground_measurement_scope`，新消费者优先读 `ground_contact`。

`resting_ratio` 只表示采样速度低于阈值的比例，界面称 Low-speed fraction；实际求解器休眠由 `frames[帧号].solver_sleep` 的 `pnw_sleep` 统计独立报告，缺失不是0。`pnw_active` 与 `pnw_sleep_rooted` 可用时另报激活／有根支撑计数，不证明整体接触质量。无输出、前后点数变化或无时间间隔且缺速度时，速度和低速比例为null、`speed_source: not_observable`，原因在 `late_motion`；位置估算按索引配对，不保证跨帧身份或连续静止。v2原始速度统计位于 `geometry_summary.physics_attributes.velocity_statistics`，检查报告顶层则直接提供 `mean_speed` / `max_speed` / `speed_source`。

缓存只使用受管独立代次目录；旧缓存与无关文件应保留。验证包含真实 Modifier 值、递归节点语义、几何属性、变换、碰撞体/实例集合依赖、场景时基与文件内容完整性。布局移动不等于物理变化，节点 operation/default、ColorRamp 和 CurveMapping 改变则必须使旧缓存失效。只读消费与写入所有权分开：另存工程可读取旧代，新烘焙创建新 owner。遇到不能可靠签名的依赖，明确拒绝或标未验证，不签 `valid`。

烘焙成功后保存输出，独立 Blender 冷开先验证实际受管缓存完整性，再比较网格/拓扑/实例变换摘要与生产证据。0.4使用 `physics-geometry-evidence-v2`，保留v1几何摘要算法，新增可读取的顶层Mesh/PointCloud/Curves/INSTANCE上全部 `pnw_` 属性、组件/域/类型/数量/值与实例原型对应关系。不要先Realize再取实例属性，同名原型属性可能遮蔽实例值。完整覆盖声明也必须一致；旧v1/缺字段或非有限值应失败，不能静默降级。数值六位量化，非逐位浮点等价。

新摘要只比较实际输出属性，不覆盖内部求解状态或嵌套原型的物理属性，也不新增raw PointCloud/Curves完整几何覆盖。刚体与布料输出速度已实证。0.8将真实模拟粒子总数写为重建表面上的INT/POINT `pnw_particle_count`；它不是表面顶点数，也不包含内部粒子速度。空表面/缺失属性计数为不可观察，不能当作0。界面分别显示诊断是否有效、属性是否可用及速度来源；位置差估算不得冒充真实速度。仅质心相等、文件存在、`FINISHED`、重复帧相等都不足以证明生产输出正确。

CLI 示例（使用本机已有 Python）：

0.4允许普通Mesh碰撞体的location/rotation_euler/rotation_quaternion/scale关键帧（单体和集合成员）。签实际绑定Action slot/channelbag、曲线控制点/handles/插值、静态通道、静态父级和parent inverse；不把当前帧矩阵当动画身份，签名不得改变帧/子帧/选择。NLA、driver、constraint、目标自身动画、动态实例原型、形态键及未知动态修改器依赖仍拒绝。仅当前5.2.1已验，未来Action layer/strip新增控制需重核。

```powershell
python '<REPO_ROOT>\tools\physics_workflow_cli_v1\cli.py' describe
python '<REPO_ROOT>\tools\physics_workflow_cli_v1\cli.py' run --request '<REPO_ROOT>\jobs\my-physics\request.json' --jobs-dir '<REPO_ROOT>\jobs\my-physics\jobs' --timeout-seconds 300
python '<REPO_ROOT>\tools\physics_workflow_cli_v1\cli.py' status --manifest '<REPO_ROOT>\jobs\my-physics\jobs\job-id\resume_plan.json'
python '<REPO_ROOT>\tools\physics_workflow_cli_v1\cli.py' cancel --manifest '<REPO_ROOT>\jobs\my-physics\jobs\job-id\resume_plan.json'
```

请求至少包含 `input_blend`、新 `output_blend`、`target_object`、`preset_id`，并明确 `parameters.frame_start/frame_end`。需要跨会话回放时用 `bake: true` 和 `verify_cold_reopen: true`。失败要同时查退出码、结构化错误与磁盘日志。

Sweep 每个候选输出、缓存和导出路径隔离，回执列实际归一化参数。run/sweep/batch 返回持久 `resume_plan`（schema `pnw.workflow.resume_plan` version 1）。`resume --manifest <绝对路径>` 先校验输入、请求、回执、输出、导出、缓存清单/文件和所需冷开证据，完整成功才跳过；失败/pending/损坏候选从原输入重放到新job，保留旧输出。输入/归一化参数变化拒绝旧计划，invalidate_cache不可重放。原任务与resume共用同机OS文件锁，竞争返回RESUME_LOCKED，退出释放；跨主机/NFS/SMB未验。不支持任意中间帧续算。保持jobs根简短以符合Windows缓存路径预算。

当前CLI的请求、文件保护和进程管理模块随源码一并提供；运行需Python 3.12/3.13及 `BLENDER_PATH` 指向已验Blender，不要求额外共享内核。不自动安装依赖。

`status`只报告可观察的阶段和候选状态；`cancel`返回请求已提交，原执行者结束自己持有的进程树并释放锁后才确认cancelled。不按manifest中的PID杀进程，不抢写者锁。resume使用新控制代次，旧取消请求不影响新作业；终态发布重新取得同一锁，避免旧作业覆盖新代。冷开验证器回执自身必须有当前v2声明，不能借worker或attempt的标签代替。未验POSIX、网络文件系统、多cancel压测与真实断电。

界面Inspection对CLI值经FloatProperty显示后的float32往返恢复已签的原参数，只有完整请求身份仍匹配时生效；真实参数或碰撞体引用变化仍使检查失效，不放宽公共CLI数值身份。

`STATIC_MESH` 临时实现实例，按同一求值对象的有效material_slots固化OBJECT材质覆盖，创建含独立静态对象的blend副本。两套UV及active/render选择、逐面材质、shader图、外部和packed图像已冷开验证；相对路径随输出重映射，当前源图像和共享数据不变。仅签目标静态对象的材质图像依赖，整个场景其它对象/缓存不在闭合声明内；外部图像保留原路径，不能称为可搬运自包含包。UDIM/序列/movie、外链材质/组/图像、外部shader脚本、缺图或未保存像素编辑明确拒绝可靠导出。

`ALEMBIC` 在目标末端临时追加Realize修改器，保留目标有效槽位后输出单一动态网格，避免旧ObjectInfo wrapper丢失OBJECT覆盖。开启uvs/face_sets；本机重导入保留多个UV图和逐面材质名，**不保留shader图、贴图、活动渲染UV选择或实例身份**。多UV采用Blender扩展，其它消费者未验。首帧/末帧为空仍可导出，全程空拒绝。未烘焙时顺序求值，旧缓存无效时拒绝。

共享Mesh原型的OBJECT材质覆盖会在Realize阶段丢失。0.6提供显式 `operation: prepare_instances` 与 UI **Prepare Prototype Copies**：选择 `instance_collection`，生成新集合及逐对象独立Mesh，把有效材质槽固化为DATA分配，保留UV、顶点组、basis/delta/负缩放。支持本地、静态、非空、无子集合的Mesh集合；parent、modifier、constraint、animation/driver、shape key、刚体和嵌套实例等明确拒绝，不静默展平。按当前Blender原生Collection Info实例行序复制，避免A2/A10、大小写、长中文名称改变原型分配。

准备操作不重绑Modifier、不改旧图、不自动Build；UI只选择新集合，之后由Build / Update接入，CLI从 `prepared_instances.prepared_collection` 取名用于后续请求。prepare不能同时bake/export/verify_cold_reopen。新集合独立可编辑，未Build也可保存重开；来源ID仅用于保留出处，不是实时依赖。源几何或槽位修改不会更新副本，shader材质数据块仍共享。副本实际几何和有效槽位变化正常使缓存失效，调色不属于几何身份变化。0.6有效槽位签名改变后旧缓存需重新烘焙，保留旧代。未经准备或准备后重新添加OBJECT覆盖的原型仍受当前导出校验约束。

`OPENVDB` 将闭合水密世界网格转surface level-set，写出后独立读取体素/transform/正负值，不等于烟雾density/velocity。复杂多壳朝向、自交和极限规模未验。

三种磁盘导出均返回`delivery_manifest`，旁车`<主文件>.pnw-export.json`（pnw.export.delivery/1）记录primary及外部依赖SHA/大小、preservation保留/损失。已有主文件或旁车拒绝覆盖；场景内静态副本无磁盘清单。CLI接收、最终签收及resume分别验证清单/主文件/依赖，缺损、篡改和旧无清单导出不可skip；签收失败同步为CLI/作业失败。失败候选重放到新路径并保留旧交付。

## 验收与接续

按变动选择原失败案例复验：无关文件哨兵、input/output 别名、参数错签、手改连线、同名对象、旋转/缩放、两球间距/接触、寿命/浮力、导出重开、缓存损坏与冷开错误预期。每项记录 pass/fail/not_run 和证据。

物理验收检查全时域或有依据的采样，分开报告几何完整性、物理合理性、视觉效果和耗时。冻结目标为 200–2000 刚体、10k–50k 粒子/顶点、120–250 帧；小场景通过不签这些规模。没有连续无穿透、稳定堆叠、真实容器液面证据时保持开发状态。

0.8刚体已修双端相对法向冲量、有限面边缘伪接触、球/静态面交错投影、重复摩擦及无支撑休眠。200球容器/坡道/帧12触发，250帧正式烘焙，F230–250重叠最大8.35e-8、位置增量/速度/旋转增量为0；独立冷开与完整序列一致。2000球稀疏输入250帧通过，不能用其代签2000密堆。公共入口确认空中低速接触球不误睡、动画平台移走后已睡三层立即下落。休眠仅启用向下重力；静态投影支撑与承重根分开，环境/根数/法线改变保守唤醒。复杂独立接触岛、保持全部检测信号不变的支撑运动未普遍验证。

历史27球1e-4失败原样保留；当前同输入回归最大重叠约9.99e-6，通过原阈值，但I3末窗最大速度约.119，不签所有密堆完全静止。旧三球、四点链和弯曲/撕裂证据仍按原范围保留，不用历史短帧性能代替本批负载。

流体压力/密度/体积守恒和真实容器填充仍待开发。布料弯曲响应与撕裂拓扑、pin保留、24帧烘焙保存冷开乱序摘要已验，不代表剪切/自碰撞/完整柔体。可见鼠标UI已按箱体预设完成；真实运行中断电恢复未验，候选取消与重放证据不能代签断电。完整规模以本批矩阵为准。

接续时记录当前实现、验证范围、未验项及恢复入口。