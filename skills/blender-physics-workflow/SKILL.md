---
name: blender-physics-workflow
description: 使用公共 CLI 与物理节点工作台制作、烘焙和核验刚体代理、布料绳索、弹性壳与粒子表面次级物理动态。主表演定稿后需要做受力响应、配置固定点与碰撞体、导出 Alembic 或 OpenVDB 表面以及核验磁盘缓存时使用。
metadata:
  version: "1.0.0"
---

# 物理模拟工作流

本技能管主表演定稿之后的物理次级响应，包括刚体散落与倾倒、布料绳索、弹性壳与粒子表面重建，属于 [动画与模拟部门](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/animation.md)。跨部门交接顺序见 [blender-work-methodology](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/SKILL.md)，通用读写与批次管理见 [blender-cli](https://github.com/tsist/blenderctl/blob/main/skills/blender-cli/SKILL.md)。

## 怎么做

### 第一步：锁死主表演，查清入口并建批次目录

先确认上游动画和碰撞体的关键帧已经定稿，再进物理环节。主动作一改，碰撞轨迹和缓存就得重算。

在 `work/audit/<batch>//` 下建独立工作目录，用公共入口查当前可用预设与参数范围，保持路径简短以留足 Windows 缓存路径长度：

```powershell
$ctl = './blenderctl.ps1'
& $ctl discover --product physics
& $ctl physics describe
```

准备一份新输出路径，把原始工程留着对照。需要手调界面参数时，先用 `physics panel-copy` 生成带面板身份的工作副本，避免误伤原对象或共享网格。

### 第二步：按受力行为选求解域与预设

看清对象要表现什么运动，再挑对应预设。所有正式流程都走原生 `GN_NATIVE` 路径：

- **刚体散落、链条与箱体倾倒（`rigid_scatter`）**：落体堆叠选 `rigid_drop_pack`，链摆选 `rigid_chain_pendulum`，单轴箱体翻倒选 `rigid_box_tipping`。球代理适合颗粒与散落件，倾倒箱体直接算真实尺寸和角速度。
- **布料、绳索与弹性壳（`cloth_rope`）**：独立旗帜、披布与自由垂布选 `cloth_banner_wind`，悬绳选 `rope_catenary_swing`，闭合三角流形软壳选 `softbody_elastic_shell`。布面需要抗折或拉裂时开启 `bending` 和 `tearing`。带骨架变形角色的贴身发裙与胸臀弹性优先走 [blender-rigging](https://github.com/tsist/blender-rigging-workflow/blob/main/skills/blender-rigging/SKILL.md) 的 `surface_dynamic_action.BAKE` / `elastic_action.BAKE`；确需转入本工作台时，先将角色碰撞体烘焙为普通关键帧网格再接入。
- **粒子喷射与表面重建（`fluid_emission`）**：喷泉颗粒与表面包裹选 `fluid_fountain_sdf`，上升烟羽体积预览选 `smoke_plume_volume`。

同类预设里调参数时走普通更新，保留作者手连的节点与断开的连线。需要切换求解域或升级旧图结构时，先看清手改内容，再在工作副本上显式提交 `build` 加 `force_rebuild: true`，新建替代图并保留旧 Wrapper 供对照。

### 第三步：配好固定点、碰撞体与原型

所有重力、风场、锚点、地面高度 `ground_z`、半径和厚度都按世界坐标结算。动手前先核对目标和碰撞体的世界矩阵，避免异常缩放带偏受力。

- **标固定点**：布料和绳索靠显式 `POINT/FLOAT` 属性 `pnw_pinned` 固定顶点。先用 `physics pin-selection` 读取或写入指定顶点索引，再跑模拟，让悬挂边稳稳挂住：
  ```powershell
  & $ctl physics pin-selection --params $pinReq --output $pinnedBlend
  ```
- **配碰撞体与延迟触发**：按需指定单个碰撞体或碰撞集合；普通网格碰撞体支持位移、旋转、缩放关键帧动画。刚体整组需要到指定帧才开始下落时，把 `parameters.activation_frame` 设为目标场景帧。
- **整理共享原型材质**：刚体实例集合里如果用了 OBJECT 级材质覆盖，先跑 `prepare_instances` 生成独立静态网格副本，把有效材质槽固化到 DATA 层，再拿返回的新集合名往下建图。

### 第四步：先跑短帧小样，再烘完整磁盘缓存

先切 15–30 帧的短区间或低规模小样试跑一轮，看大形、碰撞穿插和受力方向对不对，再放大到全帧域。这样几分钟就能排掉参数方向错误。

- **提交试跑或扫参**：把 `input_blend`、新 `output_blend`、`target_object`、`preset_id` 和 `parameters.frame_start` / `frame_end` 写成 UTF-8 JSON，用 `physics run` 执行；想对比多组刚度、风速或触发帧，用 `physics sweep` 把每个候选的输出和缓存分开落盘。
- **烘焙完整缓存**：小样方向确认后，把帧域扩到镜头全长，打开 `bake: true`，让求解结果写入受管独立代次缓存目录。大面数布料或细密堆叠按需提高 `substeps`（1–8）与迭代次数，再跑完整烘焙。

### 第五步：按需导出，长作业用清单跟踪与续跑

下游跨软件或只需要静态定格时，在请求里开启对应导出格式，系统会顺带写出 `<主文件>.pnw-export.json` 旁车清单记录完整校验和：

- **`STATIC_MESH`**：固化目标静态网格、多套 UV、着色器图与外部贴图清单到独立 `.blend`。
- **`ALEMBIC`**：输出实现后的单一动态网格序列，带多套 UV 和逐面材质槽名，方便下游直接重导入。
- **`OPENVDB`**：把闭合水密网格转为表面 level-set 体素文件，并自动读回校验体素与正负符号。

长批次运行中用 `status` 看阶段进度，想中止时用 `cancel` 提交协作取消，中断或部分失败后直接用原 `resume_plan.json` 跑 `resume` 重放未完成候选：

```powershell
& $ctl physics run --request $reqJson --jobs-dir $jobs --timeout-seconds 300
& $ctl physics status --manifest $manifest
& $ctl physics resume --manifest $manifest --timeout-seconds 300
```

### 第六步：独立冷重开核验缓存闭包

烘焙和导出完成后，打开 `verify_cold_reopen: true` 另起干净的 Blender 进程冷重开核对。核验通过才算收工：

1. 检查受管缓存清单、全部缓存文件和导出旁车清单完整无损。
2. 核对 `physics-geometry-evidence-v2` 摘要，确认顶层几何、实例变换以及 `pnw_active`、`pnw_sleep`、`pnw_particle_count` 等输出属性与生产记录一致。
3. 读诊断报告里的 `ground_contact`（区分 `instance_origins` 与 `visible_geometry`）和 `solver_sleep` / `late_motion`，同时抽查代表帧画面，亲眼确认无穿模、无炸飞、无跳帧。

## 交给下游

打包交付给灯光、渲染或合成部门，附齐以下文件与记录：

- **烘焙好的工作副本 `.blend`**（绝对路径与 SHA256），注明目标对象名、预设名、帧率与起止帧。
- **受管磁盘缓存目录与冷重开回执**（`resume_plan.json`、检查报告与缓存清单），保证下游重开即刻回放一致。
- **导出产物与旁车清单**（如有）：主导出文件与对应的 `.pnw-export.json`。
- **一段可播放的预览视频或关键帧对照图**，方便审看受力节奏和接触边界。

下游接手时，先另起进程重开 `.blend` 并跳到起止帧与关键接触帧，确认缓存加载正常、贴图和物体无偏移，再进入布光与渲染。

## 例子：给一面旗帜做 120 帧迎风摆动

接到任务：「动画部门已把旗杆动作定在 1–120 帧，给 `work/flag/pole_anim.blend` 里的 `Flag_Cloth` 加上迎风布料模拟并交缓存。」

1. **建批次与查入口**：在 `work/audit/<batch>/` 建目录，算出 `pole_anim.blend` 的 SHA256，运行 `blenderctl.ps1 physics describe` 确认 `cloth_banner_wind` 的参数名和范围。
2. **固定顶边顶点**：准备 `pin-req.json` 填入旗布靠杆一侧的顶点编号，运行 `physics pin-selection --params pin-req.json --output work/flag/pole_pinned.blend`，写入 `pnw_pinned` 固定属性。
3. **短帧小样试跑**：写一份 1–24 帧的 `test-req.json`，选 `cloth_banner_wind`，设风向 `wind_vector`、开启 `bending: true`，跑 `physics run` 看布面垂坠和飘动方向，确认顶边牢牢挂住、布面无穿杆。
4. **完整烘焙与冷重开**：把帧域改成 1–120 帧，按旗布面数调高 `substeps` 和 `constraint_iterations`，设 `bake: true` 与 `verify_cold_reopen: true`，再跑 `physics run` 生成 `work/flag/pole_flag_baked.blend` 和受管缓存目录。
5. **验收与交接**：检查回执 `ok: true`、冷重开摘要一致、关键帧画面折痕自然，把 `pole_flag_baked.blend`、SHA256、缓存目录和 120 帧预览视频交给镜头与灯光部门。

## 参考

- [求解器、参数边界、固定点与面板交接](references/solvers-and-setup.md)
- [缓存校验、诊断口径、导出格式与断点续跑](references/cache-export-and-recovery.md)
- [契约入口、源码位置与阶段实证索引](references/sources.md)
