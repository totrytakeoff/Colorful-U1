# Colorful-U1 Source Graph 架构方案

日期：2026-06-06

目标：推翻当前 `head_mode = native / ace` 的二选一模型，建立一套能长期扩展的
耗材来源图架构。新架构需要支持：

- 任意 ACE slot 映射到任意物理头。
- 单个物理头拥有多个可选耗材来源。
- native source 抽象化，为“多 native feeder 给一个头换色”预留空间。
- native source 与 ACE slot source 混合在同一个头上换色。
- 后续基于空闲头提前换料，实现接近原生 U1 换头效率的调度策略。

本文是架构设计和阶段状态文档。当前分支已经开始落地后端 source graph
基础设施，但前端 UI 还没有按新架构重构；通用 source transition 已进入
正式 preflight/rewrite 的 dry-run 闭环，尚未进入实机验证。

## 当前实现状态：2026-06-06

已完成并通过 Docker dry-run 回归：

- `source_graph.json` 基础读写、normalize、hash 和 schema 校验。
- 默认 graph 生成：
  - 4 个 `head:<n>`；
  - 4 个 `native:<n>`；
  - native source 自带 `module/channel`，目标 head 只决定 `EXTRUDER`；
  - ACE slot source 默认不猜测接线，必须通过 edge 显式配置。
- `GET /api/source-graph`、`POST /api/source-graph`、`GET /api/source-state`。
- Klipper `ace.py` 读取 source graph，并用 enabled edge 校验
  `ACE_LOAD_HEAD` / `ACE_SWAP_HEAD` 的 `HEAD/ACE/SLOT` 组合。
- Web preflight 已生成 route plan v2，并持久化 `.route_plan.json`。
- route plan 现在包含 `source_graph_hash`、`initial_state`、`tool_map`、
  structured `events[].steps` 和镜像 `commands`。
- 打印发送前会重新校验 route plan 与当前 source graph hash、edge、profile
  action 和命令字段是否一致。
- 新增 `GET /api/preflight/route-plan/validate?token=...` 和
  `POST /api/route-plan/validate`。
- 新增 source action 预览：
  - `POST /api/source-action/preview`
  - `POST /api/source-actions/preview`
- 新增通用 source transition 预览：
  - `POST /api/source-transition/preview`
  - 可根据当前 `initial_state` 生成 `unload_source -> select_head ->
    load_source/swap_source` 计划片段。
- dry-run 覆盖 `native:1 -> head:0` 的跨 source/head transition，确认
  native feeder 命令使用 source 自身的 `module/channel`，不会误用目标 head
  的 native channel。

尚未完成：

- 前端仍未按 source graph 重构，Dashboard 仍是旧 UI 逻辑为主。
- 通用 source transition 只提供 preview，不执行硬件动作。
- 正式打印 rewrite 已能消费 route plan v2 中同一 head 多 source 的
  unload/load transition，并已通过 dry-run；尚未实机验证。
- post-processor 仍保留旧 `tool_targets` / `ace_targets` fallback，后续需要
  继续收敛到 route-plan-only。
- 任意 ACE slot -> 任意 head、native + ACE 同 head 混合打印、提前换料调度
  还没有进入实机测试。

## 设计原则

1. 物理头和耗材来源必须解耦。

   物理头只代表 nozzle、extruder、sensor、heater、当前装载状态。它不再永久属于
   `native` 或 `ace`。

2. 所有耗材入口统一抽象为 source。

   native feeder、ACE slot、未来的其他外部送料装置都应该走同一套 source
   数据结构。

3. 调度器面向 source graph，而不是面向 UI 控件。

   UI 负责配置 source、head、profile、映射关系。调度器根据这些信息生成
   route plan。

4. 执行动作通过 execution profile 描述。

   不同 source 的 load、unload、swap 命令不同，不应写死在 preflight resolver
   里。

5. 状态必须区分配置、计划和真实装载。

   `configured_sources`、`planned_source`、`current_source` 是三件不同的事。
   只有 `current_source` 可信时，才允许跳过实际换料动作。

6. 新架构不再兼容旧配置作为设计约束。

   旧 `headN_mode`、`aceN_head`、旧 preflight target 可以作为迁移参考，但新分支
   不需要为了旧配置牺牲结构清晰度。

## 核心概念

### Physical Head

物理头代表 U1 上的一个实际工具头。

示例：

```json
{
  "id": "head:3",
  "index": 3,
  "label": "T4",
  "enabled": true,
  "heater": "extruder3",
  "sensor": "filament_motion_sensor e3_filament",
  "native_channel": {
    "module": "right",
    "channel": 1
  },
  "configured_sources": [
    "native:3",
    "ace:0:0",
    "ace:0:1",
    "ace:0:2",
    "ace:0:3"
  ],
  "current_source": "ace:0:3",
  "source_confidence": "known"
}
```

字段说明：

- `id`：稳定 ID，格式为 `head:<index>`。
- `index`：Klipper 物理工具头编号，0..3。
- `native_channel`：U1 原生送料通道。没有 native 通道的头可以为 `null`。
- `configured_sources`：该头允许接收的耗材来源。
- `current_source`：软件认为当前头里实际装着的 source。
- `source_confidence`：
  - `known`：软件记录和传感器状态一致。
  - `unknown`：传感器有料但来源未知。
  - `stale`：记录有 source，但传感器显示空。
  - `failed`：上次 load/unload/swap 失败，禁止自动推断。

### Material Source

source 是耗材进入系统的入口。

ACE slot 示例：

```json
{
  "id": "ace:0:2",
  "kind": "ace_slot",
  "ace": 0,
  "slot": 2,
  "label": "ACE 1 Slot 3",
  "material": "PETG",
  "brand": "Generic",
  "subtype": "Basic",
  "color": "#1e78dc",
  "ready": true,
  "configured_heads": [0, 1, 2, 3],
  "default_head": 3,
  "execution_profile": "ace_v1_slot"
}
```

native 示例：

```json
{
  "id": "native:1",
  "kind": "native_feeder",
  "head": 1,
  "label": "Native T2",
  "material": "PLA",
  "brand": "Generic",
  "subtype": "Basic",
  "color": "#dc2828",
  "ready": true,
  "configured_heads": [1],
  "default_head": 1,
  "execution_profile": "u1_native_feeder"
}
```

未来多个 native feeder 给同一个头时，不需要换架构，只需要 source ID 变得更
具体：

```json
{
  "id": "native:left:0",
  "kind": "native_feeder",
  "module": "left",
  "channel": 0,
  "configured_heads": [2],
  "default_head": 2,
  "execution_profile": "u1_native_feeder"
}
```

### Source Edge

source edge 表示某个 source 可以进入哪个 head。

```json
{
  "source": "ace:0:2",
  "head": "head:3",
  "enabled": true,
  "priority": 50,
  "physical_route": {
    "type": "ptfe",
    "merge": "u1_y_splitter"
  },
  "constraints": {
    "requires_empty_head_before_load": true,
    "allows_preload_while_other_head_prints": true
  }
}
```

这层 edge 是实现“任意 slot 映射到任意头”的关键。旧逻辑把 `ACE -> head`
作为整台 ACE 级配置，新逻辑改为：

```text
ace:0:0 -> head:0
ace:0:1 -> head:1
ace:0:2 -> head:2
ace:0:3 -> head:3
```

也可以配置成：

```text
ace:0:0 -> head:3
ace:0:1 -> head:3
ace:0:2 -> head:3
ace:0:3 -> head:3
```

从而覆盖旧单头 ACE 4 色模式。

### Execution Profile

execution profile 描述一个 source 的动作如何执行。

ACE slot：

```json
{
  "id": "ace_v1_slot",
  "kind": "ace_slot",
  "load": {
    "command": "ACE_LOAD_HEAD HEAD={head} ACE={ace} SLOT={slot}",
    "requires_empty_head": true,
    "sets_current_source": true
  },
  "unload": {
    "command": "ACE_UNLOAD_HEAD HEAD={head}",
    "requires_current_source": true,
    "clears_current_source": true
  },
  "swap": {
    "command": "ACE_SWAP_HEAD HEAD={head} ACE={ace} SLOT={slot}",
    "requires_routed_edge": true,
    "sets_current_source": true
  },
  "capabilities": {
    "can_preload": true,
    "can_swap_in_print": true,
    "requires_source_tracking": true
  }
}
```

U1 native feeder：

```json
{
  "id": "u1_native_feeder",
  "kind": "native_feeder",
  "load": {
    "command": "FEED_AUTO MODULE={module} CHANNEL={channel} EXTRUDER={head} LOAD=1",
    "requires_empty_head": true,
    "sets_current_source": true
  },
  "unload": {
    "command": "FEED_AUTO MODULE={module} CHANNEL={channel} EXTRUDER={head} UNLOAD=1",
    "requires_current_source": true,
    "clears_current_source": true
  },
  "swap": null,
  "capabilities": {
    "can_preload": false,
    "can_swap_in_print": false,
    "requires_source_tracking": false
  }
}
```

注意：`can_preload` 是能力，不代表调度器一定会提前换料。提前换料必须经过
route plan 和安全状态检查。

## Source Graph 配置文件

建议新增独立配置：

```text
/home/lava/printer_data/config/extended/multiace/source_graph.json
```

示例：

```json
{
  "version": 1,
  "heads": {
    "head:0": {
      "index": 0,
      "enabled": true,
      "native_channel": {"module": "left", "channel": 1}
    },
    "head:1": {
      "index": 1,
      "enabled": true,
      "native_channel": {"module": "left", "channel": 0}
    },
    "head:2": {
      "index": 2,
      "enabled": true,
      "native_channel": {"module": "right", "channel": 0}
    },
    "head:3": {
      "index": 3,
      "enabled": true,
      "native_channel": {"module": "right", "channel": 1}
    }
  },
  "sources": {
    "native:0": {
      "kind": "native_feeder",
      "head": 0,
      "execution_profile": "u1_native_feeder"
    },
    "native:1": {
      "kind": "native_feeder",
      "head": 1,
      "execution_profile": "u1_native_feeder"
    },
    "ace:0:0": {
      "kind": "ace_slot",
      "ace": 0,
      "slot": 0,
      "execution_profile": "ace_v1_slot"
    },
    "ace:0:1": {
      "kind": "ace_slot",
      "ace": 0,
      "slot": 1,
      "execution_profile": "ace_v1_slot"
    }
  },
  "edges": [
    {"source": "native:0", "head": "head:0", "enabled": true},
    {"source": "native:1", "head": "head:1", "enabled": true},
    {"source": "ace:0:0", "head": "head:0", "enabled": true},
    {"source": "ace:0:1", "head": "head:1", "enabled": true}
  ]
}
```

### source_graph.json 最小 schema

第一版不需要引入复杂 schema 校验库，但必须按下面的结构做显式校验。任何未知
source kind、缺失 execution profile、无效 head index、无效 ACE/slot、重复 edge
都应该在保存或读取时直接报错。

```json
{
  "version": 1,
  "heads": {
    "head:<index>": {
      "index": 0,
      "enabled": true,
      "label": "T1",
      "native_channel": {
        "module": "left",
        "channel": 1
      }
    }
  },
  "sources": {
    "<source_id>": {
      "kind": "native_feeder | ace_slot",
      "label": "Native T1",
      "material": "PLA",
      "brand": "Generic",
      "subtype": "Basic",
      "color": "#ffffff",
      "ready": true,
      "execution_profile": "u1_native_feeder"
    }
  },
  "edges": [
    {
      "source": "<source_id>",
      "head": "head:<index>",
      "enabled": true,
      "priority": 50,
      "constraints": {
        "requires_empty_head_before_load": true,
        "allows_preload_while_other_head_prints": false
      }
    }
  ],
  "profiles": {
    "<profile_id>": {
      "kind": "native_feeder | ace_slot",
      "capabilities": {
        "can_preload": false,
        "can_swap_in_print": false,
        "requires_source_tracking": true
      }
    }
  }
}
```

source kind 的必填字段：

- `native_feeder`：
  - `native` 第一版必须提供固定 `head`，或通过 edge 唯一指向一个 head。
  - 必须能解析出 `module/channel/extruder`，否则禁止生成 `FEED_AUTO`。
- `ace_slot`：
  - 必须提供 `ace` 和 `slot`。
  - `slot` 使用 0-based 内部编号，UI 可显示为 1-based。
  - 每个 ACE slot 可以有多个 edge 指向不同 head，但一次 route plan 里同一
    source 只能被一个具体 event 使用到一个具体 head。

校验规则：

- `heads` 中的 `index` 必须唯一，且 MVP 阶段限制在 `0..3`。
- edge 引用的 source/head 必须存在。
- disabled source 或 disabled edge 不能进入 route plan。
- source 的 `execution_profile` 必须存在，且 profile kind 与 source kind 一致。
- `ace_slot` source 不允许缺省 ACE 或 slot；禁止沿用旧的“唯一 ACE/唯一 slot”
  推断逻辑。
- native source 不允许在没有明确 channel 的情况下生成 load/unload 命令。
- 保存 graph 时只保存配置状态，不写入 `current_source`。真实装载状态必须来自
  `source_state` 或 Klipper `save_variables`。

### 典型拓扑表达

旧单头 ACE 4 色：

```text
ace:0:0 -> head:3
ace:0:1 -> head:3
ace:0:2 -> head:3
ace:0:3 -> head:3
```

当前已验证的 native + ACE 多头混合：

```text
native:0 -> head:0
native:1 -> head:1
native:2 -> head:2
ace:0:0 -> head:3
ace:0:1 -> head:3
ace:0:2 -> head:3
ace:0:3 -> head:3
```

目标 `2+2+2+2`：

```text
native:0 -> head:0
ace:0:0 -> head:0

native:1 -> head:1
ace:0:1 -> head:1

native:2 -> head:2
ace:0:2 -> head:2

native:3 -> head:3
ace:0:3 -> head:3
```

全自由测试拓扑：

```text
native:0 -> head:0
native:1 -> head:1
ace:0:0 -> head:0
ace:0:1 -> head:0
ace:0:2 -> head:2
ace:0:3 -> head:3
```

## Route Target

preflight 输出不再使用旧的 `kind/native/ace` 二选一 target，而输出统一 route
target。

```json
{
  "slicer_tool": 1,
  "source": "ace:0:1",
  "head": "head:3",
  "material": "PETG",
  "color": "#1e78dc",
  "operation": "swap",
  "commands": [
    "T3",
    "ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=1"
  ]
}
```

native target：

```json
{
  "slicer_tool": 0,
  "source": "native:1",
  "head": "head:1",
  "operation": "select",
  "commands": [
    "T1"
  ]
}
```

后续 native 多 source 单头时：

```json
{
  "slicer_tool": 2,
  "source": "native:left:0",
  "head": "head:2",
  "operation": "load",
  "commands": [
    "T2",
    "FEED_AUTO MODULE=left CHANNEL=0 EXTRUDER=2 LOAD=1"
  ]
}
```

## Route Plan

route plan 是一份打印任务的完整执行计划。

```json
{
  "version": 2,
  "source_graph_hash": "sha256:...",
  "initial_state": {
    "version": 1,
    "source_graph_hash": "sha256:...",
    "heads": {
      "head:0": {"current_source": "native:0", "source_confidence": "known"},
      "head:1": {"current_source": null, "source_confidence": "empty"},
      "head:2": {"current_source": null, "source_confidence": "empty"},
      "head:3": {"current_source": "ace:0:3", "source_confidence": "known"}
    }
  },
  "tool_map": {
    "0": {"source": "native:0", "head": "head:0"},
    "1": {"source": "ace:0:1", "head": "head:3"}
  },
  "events": [
    {
      "index": 0,
      "slicer_tool": 0,
      "source": "native:0",
      "head": "head:0",
      "action": "select",
      "commands": ["T0"]
    },
    {
      "index": 1,
      "slicer_tool": 1,
      "source": "ace:0:1",
      "head": "head:3",
      "action": "swap",
      "commands": ["T3", "ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=1"]
    }
  ]
}
```

route plan 是后续算法优化的入口。提前换料调度不应该直接改 resolver，而应该在
route plan 上做优化。

### route_plan.json 最小 schema

route plan 必须是可审计、可复现的打印任务计划。它不能只记录最终 G-code，还要
记录“为什么这样映射”。

```json
{
  "version": 2,
  "created_at": "2026-06-06T00:00:00+08:00",
  "source_graph_hash": "sha256:...",
  "gcode_hash": "sha256:...",
  "initial_state": {
    "version": 1,
    "source_graph_hash": "sha256:...",
    "heads": {
      "head:0": {
        "current_source": "native:0",
        "source_confidence": "known"
      }
    }
  },
  "tool_map": {
    "0": {
      "source": "native:0",
      "head": "head:0",
      "match": {
        "mode": "manual | exact | nearest | fallback",
        "confidence": 1.0
      }
    }
  },
  "events": [
    {
      "index": 0,
      "line": 1204,
      "slicer_tool": 0,
      "source": "native:0",
      "head": "head:0",
      "event_type": "tool_select | source_action | source_transition",
      "action": "select | load | unload | swap | preload | select_loaded",
      "commands": ["T0"],
      "steps": [
        {"kind": "select_head", "head": "head:0", "command": "T0"}
      ],
      "requires": {
        "source_confidence": "known | empty",
        "edge": "native:0 -> head:0"
      }
    }
  ],
  "stats": {
    "toolchange_events": 1,
    "ace_swaps": 0,
    "native_loads": 0,
    "preloads": 0
  }
}
```

route plan 校验规则：

- `source_graph_hash` 必须与发送打印时的 graph 一致；不一致则重新预检。
- 每个 event 的 source/head 必须能在 graph 中找到 enabled edge。
- 每条硬件动作命令必须由 execution profile 生成，不能由 UI 拼接。
- `ACE_LOAD_HEAD` / `ACE_SWAP_HEAD` 必须包含完整 `HEAD/ACE/SLOT`。
- `FEED_AUTO` 必须包含明确 `MODULE/CHANNEL/EXTRUDER`。
- `preload` event 只能由调度器生成，不能由用户手动 target 直接生成。
- 任何 `confidence = unknown/failed` 的 head 参与 event 时，必须阻止打印发送，
  除非 route plan 明确包含人工恢复后的确认状态。

## 状态模型

每个 head 的 source 状态：

```json
{
  "head": "head:3",
  "sensor_filament": true,
  "current_source": "ace:0:1",
  "source_confidence": "known",
  "last_action": "swap",
  "last_error": null
}
```

状态解释：

- `empty`：传感器无料，`current_source = null`。
- `known`：传感器有料，且 source 记录可信。
- `unknown`：传感器有料，但 source 不知道。相当于当前 ghost head。
- `stale`：source 记录存在，但传感器无料。打印开始前应清理。
- `failed`：上次 source action 失败，需要恢复，不允许自动继续。

调度器规则：

- `known` 且 `current_source == planned_source`：可以跳过换料。
- `known` 且 `current_source != planned_source`：必须执行 unload/load 或 swap。
- `empty`：可以执行 load。
- `unknown`：禁止自动 swap，要求用户恢复。
- `stale`：清理 source 后按 empty 处理。
- `failed`：禁止继续，要求 recover。

## UI 设计边界

Dashboard 不再配置 `head mode`，而配置 source graph。

建议页面结构：

```text
Toolheads
  Head T1
    current source
    allowed sources
    native feeder source
    attached ACE slots

Sources
  Native T1
    material/color
    allowed head

  ACE 1 Slot 1
    material/color
    allowed heads
    default head
```

最小 MVP UI：

- 每个 ACE slot 卡片上选择 target head。
- 每个 native source 卡片显示固定 native head。
- 每个 head 卡片显示可用 sources 和 current_source。
- 保存 source graph 时不立即重启 Klipper；先保存配置，再显式 apply/restart。

## API 设计

新增：

```http
GET /api/source-graph
POST /api/source-graph
GET /api/source-state
POST /api/source-action/preview
POST /api/source-actions/preview
POST /api/source-transition/preview
GET /api/preflight/route-plan
GET /api/preflight/route-plan/validate
POST /api/route-plan/validate
POST /api/route-plan/preview
POST /api/route-plan/print
```

`GET /api/source-graph` 返回配置图。

`GET /api/source-state` 返回实时状态：

```json
{
  "heads": {...},
  "sources": {...},
  "edges": [...],
  "errors": []
}
```

`POST /api/route-plan/preview` 上传或引用 G-code，返回 route plan 和 UI 映射。

`POST /api/route-plan/print` 使用指定 route plan 打印。

当前已实现的是 preflight 路径中的 route plan 生成、保存、校验，以及 source
action/transition preview。`POST /api/route-plan/preview` 和
`POST /api/route-plan/print` 仍是目标 API 形态，尚未作为独立稳定入口落地。

## 与旧代码的替换关系

需要被替换或重构的旧模块：

- `headN_mode` / `aceN_head` 配置读取。
- Web `toolheadMode` / `aceTarget` 控件。
- `_build_mixed_resolver()`。
- `_live_loadout_from_parsed()` 的 target 生成。
- `tool_targets` 旧格式。
- `post_process_virtual_toolheads.py` 中基于 `kind=native/ace` 的 rewrite 逻辑。
- Klipper `_check_routed_head()` 的 ACE 级路由校验。
- Klipper `_route_status()` 的 `head_modes` / `ace_targets` 输出。

需要保留并迁移的能力：

- `head_source` 安全状态。
- ghost head 检查。
- stale `head_source` 清理。
- explicit `HEAD/ACE/SLOT` 命令要求。
- load failed recover 防线。
- dry-run 回归框架。
- preflight source map / swap stats / optimization suggestion。

## 开发阶段

### Phase A：文档和 schema

状态：基本完成，仍需随着实现继续维护 schema 细节。

- 完成本文。
- 定义 `source_graph.json` schema。
- 定义 `route_plan.json` schema。
- dry-run 增加 source graph fixture。

验收标准：

- 只新增文档、schema 或 dry-run fixture，不修改实机 Klipper 动作逻辑。
- 能用至少 4 个 fixture 表达：
  - old single ACE head 4 slots。
  - current native + ACE mixed MVP。
  - target `2+2+2+2`。
  - one head mixed native + ACE sources。
- fixture 中每个 route edge 都能被 schema 校验发现引用错误。
- 明确标记 0-based 内部 slot 与 UI 1-based 显示，避免再次出现 slot4 固定进料
  这类映射问题。

### Phase B：后端 source graph 解析

状态：已完成第一版后端实现，并通过 dry-run 回归。

- Web backend 读取 `source_graph.json`。
- 如果文件缺失，生成默认 graph。
- 提供 `GET /api/source-graph`。
- 不改 rewrite，不改 Klipper。

验收标准：

- `GET /api/source-graph` 能返回完整 graph、hash、校验 warning/error。
- `POST /api/source-graph` 只保存 graph，不自动重启 Klipper。
- 默认 graph 只能表达当前物理上可确认的 native feeder，不能自动猜测 ACE slot
  接到了哪个 head。
- dry-run 中 source graph 读写不会改变现有 preflight 输出。
- 后端日志能打印 source/head/edge/profile 的解析结果，方便实机前人工确认。

### Phase C：preflight 内部切换到 source graph

状态：已完成核心后端路径。preflight 已生成 route plan v2，打印发送前会校验
route plan 与当前 source graph。旧 `tool_targets` fallback 仍存在，需要继续
收敛。

- resolver 输入改为 source graph。
- 输出 route target：
  ```json
  {"source": "ace:0:1", "head": "head:3"}
  ```
- 仍然只生成当前旧等价命令，保证 dry-run 通过。

### Phase D：ACE slot 任意映射到任意 head

状态：Klipper edge 校验和后端 graph 表达能力已具备；前端配置 UI 和实机验证
尚未完成。

- Klipper 支持 slot-level target head。
- Web UI 支持每个 ACE slot 选择 head。
- `ACE_SWAP_HEAD HEAD=X ACE=A SLOT=S` 按 source graph edge 校验。
- dry-run 覆盖：
  ```text
  ace:0:0 -> head:0
  ace:0:1 -> head:1
  ace:0:2 -> head:2
  ace:0:3 -> head:3
  ```

### Phase E：单 head 多 ACE source

状态：架构可表达，旧单头 ACE 4 色路径已通过实机 MVP；新 source graph 路径
尚未完全替换旧 UI/rewrite fallback。

- 同一 head 可配置多个 ACE slots。
- 支持旧单头 ACE 4 色能力。
- source state 必须正确维护 `current_source`。

### Phase F：native source 抽象

状态：已完成后端第一版。native source 已携带自己的 `module/channel`，可生成
`FEED_AUTO MODULE=... CHANNEL=... EXTRUDER=<target head>`，并通过 dry-run 覆盖
`native:1 -> head:0` 的跨头 transition preview。

- native feeder 也变成 source。
- 允许 route target 指向 native source。
- 初期 native source 仍只允许固定 head。

### Phase G：native + ACE 同 head 混合

状态：已完成 dry-run 执行闭环。`POST /api/source-transition/preview` 能根据
当前 head 的 `current_source` 生成 `unload_source -> select_head ->
load_source/swap_source` 计划片段；正式 preflight route plan 已复用同一套
planner，rewrite 后最终 G-code 能包含对应 transition 命令。尚未实机验证。

- 同一 head 同时允许 native source 和 ACE slot source。
- 定义 native -> ACE、ACE -> native 的 unload/load 动作。
- 禁止 source_confidence 不可靠时自动换料。

### Phase H：提前换料调度

状态：未开始。需要等通用 transition 能在打印路径稳定执行后再做。

- 在 route plan 上做 lookahead。
- 判断下一 source 是否能提前装到空闲 head。
- 输出 preload action。
- 不改变 G-code 语义，只减少 toolchange 时等待时间。

## 实施安全边界

Phase A/B/C 属于无硬件动作阶段。即使 dry-run 通过，也不能把这些阶段的中间态
直接刷到实机测试换料。

进入任何会触发 load/unload/swap 的阶段前，必须满足：

- dry-run 覆盖目标拓扑。
- route plan command preview 与最终上传 G-code 一致。
- Klipper 侧按 source graph edge 做二次校验。
- 所有硬件动作仍要求显式参数，不恢复任何隐式推断。
- load/unload/swap 失败时不能更新 `current_source = target_source`。
- 断电或 Klipper restart 后，传感器状态与 source state 冲突时必须进入
  `unknown/stale/failed`，不能自动假定成功。

第一轮实机测试顺序：

1. 只读 graph 和 source state。
2. Dashboard 手动 load/unload 单个 native source。
3. Dashboard 手动 load/unload 单个 ACE slot 到指定 head。
4. 单 head 多 ACE slot 换料。
5. ACE slot 分别映射到不同 head。
6. native + ACE 同 head 换料。
7. 带 route plan 的真实打印。

## 风险点

1. 同一 head 多 source 会放大 `head_source` 错误影响。
2. native source 缺少 ACE 那样的 slot 级 source 记录，需要建立自己的 tracking。
3. 提前换料会引入热端温度、空闲 head、耗材残留、失败恢复等新状态。
4. 如果 UI 配置和 Klipper 校验不同步，会出现“预检能发、Klipper 拒绝”的问题。
5. 任意 slot 映射到任意 head 必须严格依赖用户确认物理 PTFE 接线。

## 当前建议

下一步不再是从 schema 开始，而是把已经完成的后端基础设施收敛成可执行闭环：

1. 收敛 post-processor：
   - route plan v2 作为主输入；
   - 继续减少 `tool_targets` / `ace_targets` fallback；
   - 继续补充同一 head 多 source 的 transition rewrite 回归场景。
2. 继续强化正式 preflight route plan 的 source transition：
   - 保持最终 G-code 与 preview 命令一致；
   - route plan 校验通过后才允许发送；
   - 增加 native -> ACE、native -> native、ACE -> native、ACE -> ACE 的组合覆盖。
3. 补 Web G-code 机型/方言安全校验：
   - 阻止 P1S/Bambu 风格文件；
   - 阻止 `G380`、`M620` 等当前 U1 Web 路径不安全命令；
   - 对 rewrite 后最终文件做二次校验。
4. 完成 Dashboard source graph UI 重构：
   - head、source、edge 三层配置；
   - 保存 graph 不自动重启；
   - 实机 apply 前显示完整 diff 和命令预览。
5. 再进入小步实机验证：
   - 只读 graph/source state；
   - 单 source 手动 load/unload；
   - 单 head 多 ACE source；
   - native + ACE 同 head；
   - 最后才做真实打印。

提前换料调度必须排在这些之后。当前首要目标仍是把基础 source transition 做到
可审计、可回滚、dry-run 和实机行为一致。
