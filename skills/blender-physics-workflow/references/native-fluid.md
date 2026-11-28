# Blender 5.3 原生液体与烟气

Windows Blender `5.3.0` 构建 `02f0899d78e3` 的当前候选版共享一个插件源码。液体使用 `native_liquid`，烟气用 `native_gas`。可以用已保存的域，缓存设为 All 或 Modular、帧偏移 0。液体开启 Mesh，烟气采用 OpenVDB 数据。若要交固定变换的烟气导向序列，关闭 Adaptive Domain。较早的粒子和表面重建流程仍用各自的 `GN_NATIVE` 预设。

## 发现入口、试形与冷开

```powershell
$project = '<PROJECT_ROOT>'
$ctl = Join-Path $project 'blenderctl.ps1'
& $ctl --blender-version 5.3.0 physics describe
& $ctl --blender-version 5.3.0 discover --product physics --action pnw.native_fluid
```

读 `data.native_fluid` 的版本、可调项和操作。`physics run --request` 接收 UTF-8 JSON，请求含 `input_blend`、`preset_id`、`target_object`、`modifier_name`、`operation`、`parameters` 中的实际 `frame_start` 与 `frame_end`。可以给 `expected_sha256` 锁定接手版本，给 `preserve_objects` 核验指定网格的几何与世界变换。公共 `request` 信封可用 `schema_version: "1.0"`、`command: "physics.run"` 和 `params`。

| 目的 | 操作与输出 |
| --- | --- |
| 查域、帧域、域身份和缓存代次 | `inspect`。从 `data.inspection` 读 `cache_state`、`cache_valid` 和 `generation` |
| 接管、调参、重烘、分离、接回烟导向 | `build`、`adopt`、`parameters`、`bake`、`invalidate_cache`、`guide_density`。各写一个新的绝对 `output_blend`。`parameters` 可给对应预设的 `native_settings` |
| 单帧出图 | `preview`，填 `preview.frame`、新的绝对 `preview.path`，可选 `width` 128–800 和 `samples` 1–32。按工作文件内的相机出图 |
| 取液体定格 | `export`，填 `export_format: "STATIC_MESH"`、`export_frame`、新的绝对 `.blend` `export_path` |
| 交烟气 | `guide_density` 创建或复用导向。`export_density` 填 `native_density.guide_name` 与新的绝对 `native_density.output_directory`。交 `FloatGrid` / `FOG_VOLUME` 的 `density`、帧号、网格变换和校验清单 |

新输出的父目录先创建，使用尚不存在的目标路径。CLI 对工作副本和密度输出自动另起进程冷开再发布。写入完读 `data.independent_cold_reopen`、`artifacts` 和 `data.resource_evidence`。用 `operation: "bake"` 烘焙。原生工作副本候选共用 `sweep`、`batch` 和 `resume/status/cancel`，支持以 `data.resume_plan` 恢复。候选恢复会分配新的输出和缓存位置。

## 从裸网格搭域

在带场景框架的源工作副本上给 `target_object` 一个尚未占用的名字，给 `modifier_name` 一个域修改器名，用 `build` 搭域或用 `bake` 搭域并烘焙。增加下面的请求字段，网格和风场名称按保存的场景填写。

```json
{
  "native_setup": {
    "inflows": ["Emitter"],
    "effectors": ["Character Collision"],
    "domain_margin": 0.4,
    "cache_type": "MODULAR"
  },
  "force_field_objects": ["Wind"],
  "native_settings": {"resolution_max": 32, "use_noise": true, "vorticity": 0.1},
  "archive_mode": "none"
}
```

域包围盒取作用范围内逐帧求值的网格并添加余量。碰撞代理按 Armature、细分或修形、Fluid 的顺序求值。已接入的静态 Wind、Turbulence、Vortex 与逐帧蒙皮代理的几何都进入缓存依赖指纹。液体调参可给 `viscosity` 、`surface_tension` 和 `mesh_scale`。烟气调参可给 `vorticity`、`use_noise`、`noise_scale`、`noise_strength` 和 `use_adaptive_domain`。

需要更改 Modular 的表面或噪波而保留已签收数据时，将 `input_blend` 指向已烘焙副本，去掉搭域字段，提交 `operation: "bake"`、`reuse_data: true` 和相关 `native_settings`。新代次复制验证过的 Data 与 Config，液体只烘 Mesh，烟气只烘 Noise。Modular 默认不复制归档，All 默认留副本，可用 `archive_mode: "none"` 或 `"copy"` 明确选择。

## 三件样例的实跑方式

在工作室的 5.3 宿主可用下面的原件复验。接新项目时替换源文件、域名、修改器和帧域。每条命令保存请求与回执，输出写入同一新批次。

```powershell
$fixtures = Join-Path $project 'work/audit/<batch>/'
$work = Join-Path $project ('work/audit/<batch>/' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$jobs = Join-Path $work 'jobs'
New-Item -ItemType Directory -Path $work | Out-Null

function Invoke-Fluid {
    param([string]$Label, [hashtable]$Payload)
    $requestFile = Join-Path $work "$Label.request.json"
    $resultFile = Join-Path $work "$Label.result.json"
    if ((Test-Path -LiteralPath $requestFile) -or (Test-Path -LiteralPath $resultFile)) {
        throw "Choose a new label or a new batch. $Label"
    }
    $Payload.expected_sha256 = (Get-FileHash -LiteralPath $Payload.input_blend -Algorithm SHA256).Hash.ToLowerInvariant()
    $Payload | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $requestFile -Encoding utf8
    $reply = (& $ctl --blender-version 5.3.0 --jobs-dir $jobs --timeout 180 physics run --request $requestFile) -join "`n"
    $exit = $LASTEXITCODE
    $reply | Set-Content -LiteralPath $resultFile -Encoding utf8
    $parsed = $reply | ConvertFrom-Json
    if ($exit -ne 0 -or !$parsed.ok) { throw "$Label $($parsed.error.code) $($parsed.error.message)" }
    $parsed
}
```

### 瓶身飞溅

先看正侧视与瓶壁接触，从真实烘焙选帧。以比例编辑或雕刻修薄液翼，保留独立的定格网格供接手。重新烘焙后核对旧手修的指纹，再把候选帧固化为另一份网格。

```powershell
$bottle = Join-Path $fixtures 'bottle-guarded/bottle-artist-rejoined.blend'
$tuned = Join-Path $work 'bottle-tuned.blend'
$baked = Join-Path $work 'bottle-baked.blend'
$pose = Join-Path $work 'bottle-f011.blend'
$liquid = @{
    preset_id = 'native_liquid'; target_object = 'Liquid Surface'; modifier_name = 'Liquid Domain'
    parameters = @{frame_start = 1; frame_end = 14}; resource_profile = 'light'
    preserve_objects = @('Bottle Splash | Artist Handoff', 'Bottle Splash | Rim Droplets')
}
$baseline = Invoke-Fluid 'bottle-inspect' ($liquid + @{operation='inspect'; input_blend=$bottle})
$tunedResult = Invoke-Fluid 'bottle-parameters' ($liquid + @{
    operation='parameters'; input_blend=$bottle; output_blend=$tuned; native_settings=@{resolution_max=48}
})
$bakeResult = Invoke-Fluid 'bottle-bake' ($liquid + @{operation='bake'; input_blend=$tuned; output_blend=$baked})
$poseResult = Invoke-Fluid 'bottle-export' ($liquid + @{
    operation='export'; input_blend=$baked; export_format='STATIC_MESH'; export_frame=11; export_path=$pose
})
```

回执中检查 `preserved_objects` 前后一致，`independent_cold_reopen.editable_mesh` 对应新取帧。短档 R48 适合重算轮廓，近景薄厚与离体水滴以正侧画面复核。

### 固定透明杯

保留原始 FLIP 时间轴，在固定侧视比较进液和停流。液面高度、范围、杯外边界与闭合网格体积用真模拟采样。缝隙与折射影响水线时，可按采样单独制作晚段光学展示，注明采用帧段和原模拟证据。

```powershell
$cup = Join-Path $fixtures 'cup-timed/work.blend'
$cupLook = Join-Path $fixtures 'cup-timed/cup-optical-interface.blend'
$liquidCup = @{
    preset_id='native_liquid'; target_object='Liquid domain'; modifier_name='Mantaflow liquid'
    resource_profile='light'; parameters=@{frame_start=1; frame_end=100}
}
$cupFacts = Invoke-Fluid 'cup-inspect' ($liquidCup + @{operation='inspect'; input_blend=$cup})
$earlyImage = Invoke-Fluid 'cup-f035' ($liquidCup + @{
    operation='preview'; input_blend=$cup; preview=@{frame=35; path=(Join-Path $work 'cup-f035.png'); width=320}
})
$lateImage = Invoke-Fluid 'cup-f100' ($liquidCup + @{
    operation='preview'; input_blend=$cupLook; preview=@{frame=100; path=(Join-Path $work 'cup-f100.png'); width=320}
})
$cupSurface = Invoke-Fluid 'cup-surface' ($liquidCup + @{
    operation='export'; input_blend=$cup; export_format='STATIC_MESH'; export_frame=100
    export_path=(Join-Path $work 'cup-water-f100.blend')
})
```

样例 F36 关流，F76 至 F100 采样高差约 58.8 → 19.8 mm。晚段展示用 F60–100 的真实采样驱动形态键，真模拟末帧网格为 8,334 顶点、16,664 面。

### 绕物烟团

在完整原生气体缓存上继续调控制体或导向强度。`Rasterize Points` 把修改后的真实密度样本写回同一体素变换。细丝在派生节点图上继续编辑。固定灯光和相机，原生与引导各出同一帧，并从斜角核对烟与道具的空间关系。

```powershell
$smoke = Join-Path $fixtures 'smoke-r96/smoke-contact.blend'
$guide = Join-Path $work 'smoke-guide.blend'
$density = Join-Path $work 'density'
$gas = @{
    preset_id='native_gas'; target_object='Editable Mantaflow gas domain'; modifier_name='Mantaflow'
    resource_profile='light'; parameters=@{frame_start=1; frame_end=48}
}
$guideResult = Invoke-Fluid 'smoke-guide' ($gas + @{
    operation='guide_density'; input_blend=$smoke; output_blend=$guide
    native_density=@{guide_name='Quality Smoke Guide'; guide_strength=0.61}
})
$smokePreview = Invoke-Fluid 'smoke-f044' ($gas + @{
    operation='preview'; input_blend=$guide; preview=@{frame=44; path=(Join-Path $work 'smoke-f044.png'); width=320}
})
$densityResult = Invoke-Fluid 'smoke-vdb' ($gas + @{
    operation='export_density'; input_blend=$guide
    native_density=@{guide_name='Quality Smoke Guide'; output_directory=$density}
})
```

比较 `data.guide_state` 与其冷开副本。导向强度调整应保留图指纹、细丝密度参数和控制体位置。检查 `density-sequence.json` 的 48 个实档、`density` 网格、变换与哈希，以及冷开 `volume_seek`。更换烟气烘焙代次后先 `guide_density` 刷新密度源，再导出至新目录。

## 面板、缓存与资源恢复

- 用面板时先在加载对应插件的 Blender 中保存工作副本，选域进入 **Physics Nodes → Native Fluid**。依次 **Adopt Domain**、**Bake**、**Inspect**。改原生域设置前用 **Detach Managed Cache** 留住旧代次和归档。液体用 **Save Editable Mesh**，烟气用 **Build / Find Guide** 与 **Export Density Sequence**。
- 预览优先试形。短档 `light` 硬上限 768 MiB，至少 7.25 GiB 可用物理内存才启动。标准档 `standard` 硬上限 1.5 GiB，启动门槛 8 GiB。两档串行、两逻辑核与两线程，运行保留 6 GiB 桌面余量。记录烘焙耗时、峰值提交与缓存大小。资源不足时读 `resources.json`，保留已提交结果，等余量恢复再用新输出路径。
- `External` 表示外部缓存。保留原工程，在未烘焙的独立工作副本建立受管代次。`stale` 或 `NATIVE_FLUID_CACHE_*` 先核身份、清单、帧与实际缓存位置。原生域属性变更可清理当前活动缓存，安全分离后再调。只在签名帧有效时预览或导出。
- 交接保存的 `.blend`、请求与回执、相关活动缓存及同代归档。导向工程可能依赖作业里的 `guide-source`。烟序列随 `density-sequence.json` 使用，其 Frame Start、Offset、Duration 和模式由清单提供。精确预设与支持项始终以 `physics describe` 为准。[实跑回执和性能](<work/audit/<batch>/>) · [三件作品与面板接手](<work/audit/<batch>/>)
