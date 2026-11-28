# Blender 5.3 原生布料与刚体

在源工程的工作副本上运行 `blenderctl.ps1 physics run --request <请求.json>`。参数放在 UTF-8 JSON，给出已保存源工程、新工作副本的绝对路径，以及 `preset_id`、`target_object`、`operation` 和 `parameters.frame_start/frame_end`。使用 `build` 配置、`bake` 落盘或 `inspect` 验证现有缓存。

## 原生角色布料

`preset_id` 填 `native_cloth`，`target_object` 填主要服装。示例请求字段如下。

```json
{
  "native_setup": {
    "colliders": ["Character Collision"],
    "layers": [
      {"object": "Skirt Lining", "pin_group": "Waist Pin", "use_self_collision": true},
      {"object": "Skirt Outer", "pin_group": "Waist Pin", "use_sewing_springs": true}
    ]
  }
}
```

从里向外排层。接触代理先做 Armature、细分或修形，再交给 Collision。解算外层时读已烘焙内层。缝纫需要网格里的缝纫边。封闭的充气网格可设置 `use_pressure` 和 `uniform_pressure_force`。可按层指定 `quality`、`mass`、`distance_min`、`self_distance_min` 或 `modifier_name`。交付 Blend 时保留作业中的 BPHYS 文件和冷开回执。

## 原生 Bullet 与破碎

`preset_id` 填 `native_rigid_body`。以下示例从块体生成碎片，接收已关键帧化的撞击物。

```json
{
  "native_setup": {
    "bodies": [
      {"object": "Floor", "type": "PASSIVE", "collision_shape": "MESH"},
      {"object": "Striker", "kinematic": true, "collision_shape": "CONVEX_HULL"}
    ],
    "fracture": {"object": "Fracture Block", "source_limit": 8, "seed": 0},
    "alembic": true
  }
}
```

可在 `native_setup.constraints` 指定场景里的 Empty（`object`）和两端刚体名（`object1`、`object2`），支持 `FIXED`、`HINGE`、`use_breaking` 和 `breaking_threshold`。场景已有分块时，直接在 `bodies` 列出来。新破碎依赖本机已安装的官方 Cell Fracture。后台使用可导入的 Blender 官方扩展 `bl_ext.blender_org.cell_fracture`。用无现有变换动画、无约束、无父级的动态模拟网格接收烘焙结果。设置 `alembic` 时同时为动态对象写同帧段 Alembic。

## 恢复与下游核对

`sweep` 和 `batch` 可为上述预设生成多个新候选。用 `data.resume_plan` 指向作业清单，再跑 `physics status`、`cancel` 或 `resume`。公开入口另起进程核验 `data.independent_cold_reopen` 的乱序取帧、缓存或 Alembic 指纹。用代表帧实际观察角色与碎片接触边界。

