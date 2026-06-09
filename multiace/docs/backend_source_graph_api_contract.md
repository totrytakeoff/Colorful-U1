# Colorful-U1 Backend Source Graph API Contract

日期：2026-06-07

本文记录后端 source graph 收口后的前端/API 使用边界。前端重构必须以这里的
接口为准，避免重新接回旧 `head_mode` / `ace_targets` 路径。

前端重构总体方案见：
`multiace/docs/frontend_ui_rewrite_plan.md`。

## 核心原则

- source graph 是配置事实来源。
- source state 是运行时事实来源。
- route plan 是打印发送事实来源。
- `.route_plan.json` 是打印发送时唯一可接受的持久化计划来源；`source_map`
  不能反推 route plan。
- `head_modes`、`ace_targets`、`primary_head` 只允许作为旧状态展示或迁移参考，
  不能作为新 UI 的配置模型，也不能作为打印路由依据。
- 手动硬件动作必须使用显式 `HEAD/ACE/SLOT` 或 source graph profile 生成的命令。
- 打印阶段不能再接受 `tool_targets` 覆盖；所有人工映射修改必须先更新 route plan。

## Dashboard 状态

### `GET /api/state`

用途：兼容 Dashboard 的聚合状态。

可继续读取：

- `aces`
- `toolheads`
- `wiring`
- `active_device`
- `notifications`
- `route` 中的旧字段只读展示

前端重构注意：

- 不要用 `state.route.head_modes` 作为工具头模式配置。
- 不要用 `state.route.ace_targets` 作为 ACE 归属配置。
- 新 UI 的工具头/耗材来源配置应读取 `/api/source-graph`。

## Source Graph 配置

### `GET /api/source-graph`

用途：读取当前配置图和校验元数据。

返回：

- `graph`：当前 source graph。
- `meta.hash`：配置 hash，route plan 会绑定该 hash。
- `meta.errors` / `meta.warnings`：配置校验结果。

### `POST /api/source-graph`

用途：保存 source graph。

约束：

- 只保存配置。
- 不重启 Klipper。
- 不移动硬件。
- 不写入当前装载状态。
- 保存后后端会尝试执行 `MULTIACE_REFRESH_SOURCE_GRAPH`，让 Klipper 重新读取
  `source_graph.json`。该命令只刷新内存中的 source graph，不执行任何送料、
  换料或打印动作。若刷新失败，保存结果仍返回 `ok=true`，并在 `refresh.error`
  中暴露错误。

前端要求：

- 保存 graph 后应重新读取 `/api/source-graph` 和 `/api/source-state`。
- 保存 graph 后已有 route plan 可能失效，打印前必须重新 preview/validate。

### Source Execution

每个 source 可带 `execution` 对象。当前已支持：

```json
{
  "execution": {
    "preload_length_mm": 950
  }
}
```

语义：

- `preload_length_mm` 是 source slot 从入口预进料到四通/目标路径入口的长度，
  单位 mm。
- `native:<n>` 和 `ace:<ace>:<slot>` 都按 source 独立配置。
- 合法范围为 `0..3000`。
- 对 ACE source，`0` 表示禁用 ACE 自动预进料。
- 该字段是 source 状态/执行参数，不代表工具头已经 loaded；工具头是否 loaded
  仍只由 `source-state.heads[head:N].current_source/source_confidence` 判断。

## Source State

### `GET /api/source-state`

用途：通过 source graph 解释当前 head/source 状态。

关键字段：

- `heads[head:N].current_source`
- `heads[head:N].source_confidence`
- `sources`
- `meta`

状态语义：

- `known`：可用于 route plan。
- `empty`：可执行 load。
- `unknown` / `stale` / `failed`：必须恢复后重新 preflight。
- `exhausted`：ACE slot 已 empty 但路径可能有余料，必须走断料/余料恢复流程。

## 手动动作预览

### `POST /api/source-action/preview`

输入：

```json
{"source": "ace:0:1", "head": "head:3", "action": "load"}
```

用途：预览单个 profile action，不执行硬件动作。

### `POST /api/source-actions/preview`

用途：批量预览 profile actions，返回 route-plan fragment。

### `POST /api/source-transition/preview`

用途：根据当前 source state 预览从当前 source 到目标 source 的 transition。

约束：

- 只预览。
- 不发送 G-code。
- 不改变硬件状态。

## 打印发送流程

正式流程：

1. `POST /api/route-plan/preview`
2. 如需人工改映射：`POST /api/route-plan/remap`
3. 打印前校验：`GET /api/preflight/route-plan/validate`
4. 发送打印：`POST /api/route-plan/print`
5. 查询状态：`GET /api/preflight/print/status`

兼容入口：

- `POST /api/preflight` 等价于 route-plan preview。
- `POST /api/preflight/print` 只允许 `mode=slicer`，且不接受 `tool_targets`。

已禁用入口：

- `POST /api/upload-and-print` 返回 410。
- `mode=optimize` / `mode=layer` 打印发送返回 400。

### `POST /api/route-plan/remap` 前端约束

`remap` 是预览阶段唯一允许人工改 tool/source 映射的入口。

前端必须遵守：

- 只在用户点击“应用映射”时调用。
- 请求体必须包含本次 G-code 使用的所有 slicer tools 的完整 `tool_targets`。
- 不要在用户每点一个 source 时自动调用，否则半完成映射会被后端按
  `manual mapping missing T...` 拒绝。
- remap 成功后必须重新执行
  `GET /api/preflight/route-plan/validate?token=...`。
- remap 成功前不得解锁发送打印。
- 用户修改本地映射后，旧 validate 结果应视为失效。

推荐 UI 状态：

```text
preview 后未映射完整: 需要手动映射 T...
用户选齐但未 remap: 待应用
remap + validate ok: ok
validate failed: blocked
```

## 手动 G-code / 插件边界

以下入口会做安全校验：

- `POST /api/macro`
- `POST /api/macro-async`
- `POST /api/macro-batch`
- `POST /api/plugin-api/gcode`

已阻止：

- `SET_ACE_MODE`
- `ACE_RUN_MODE_SWITCH`
- 旧 `ACEB__Load_*`
- 旧 `ACEC__Load_T*`
- 旧 `ACEF__Mode_Normal`
- 旧 `ACEF__Mode_Multi`

显式参数要求：

- `ACE_LOAD_HEAD` 必须带 `HEAD/ACE/SLOT`。
- `ACE_SWAP_HEAD` 必须带 `HEAD/ACE/SLOT`。
- `ACE_TEST` / `ACE_SEQ` / `ACE_PRELOAD` 的 `PLAN` 必须是显式
  `HEAD:ACE:SLOT` 条目。
- `FEED_AUTO` 必须通过 `MODULE/CHANNEL/EXTRUDER` 一致性校验。

## 旧字段收口状态

仍可能出现但不应新增依赖：

- `state.route.mode`
- `state.route.primary_head`
- `state.route.ace_targets`
- `state.route.head_modes`

保留原因：

- 当前旧前端仍有读取。
- 实机调试时需要观察旧配置残留。
- Klipper 底层仍有少量 source graph 不可用时的 fallback。

已从新输出移除：

- `source_map.route`

已从打印路径移除：

- 从 `source_map` fallback 重建 route plan。

移除条件：

1. 前端 UI 完成 source graph 重构。
2. Dashboard 不再读取旧 route 字段。
3. Klipper 启动和 print-start 检查在无 source graph 时改为明确错误，而不是
   fallback 到旧 `head_modes`。
4. post-processor CLI 旧 `ace_targets/tool_targets` fallback 被隔离到离线工具模式。
