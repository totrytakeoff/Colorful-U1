# Colorful-U1 Unified Slot / Toolhead Flow

日期：2026-06-12

本文定义后续进料、退料、换料重构必须遵守的统一语义。目标是彻底拆开
`source slot` 和 `toolhead` 两层职责，避免 native slot、ACE slot、工具头状态
继续互相污染。

## 当前状态：2026-06-13

统一语义仍然有效，且不因 stock native feeder 的机械限制而改变：

- `source slot` 负责入口、预进料、推料、回抽、完全退料等 source 路径状态。
- `toolhead` 负责传感器、当前装载 source、进退工具头、prime/flush 等工具头状态。
- `preload_finish` 仍只表示 source 预进料完成，不能表示工具头已 loaded。
- `head.current_source` 只能在完整 load post-check 成功后写入；unload/retract 任一
  阶段失败时不能清空。

最新实机观察：

- stock native feeder 在反向回抽时可能存在机械打滑。短距离
  `FEED_AUTO_RETRACT` 测试中曾出现电机转动但耗材位移没有达标的日志。
- 用户切断耗材后再测试，native source 入口状态可以变化，但这不能证明“带负载从
  工具头/四通路径回抽”已可靠。
- 用户决定后续仍按预设架构推进，并可能改造 native feeder 机械结构来支持可靠回抽。

因此本文档的 adapter 契约不降级：

- Native Slot Adapter 仍必须实现 `retract_source_to_junction` 和
  `full_unload_source`。
- 当前 stock 硬件只能标记为“reverse capability 待实机逐项验证”，不能在软件语义上
  改成 forward-only。
- 新增的 `toolhead_sync_retract_length_mm` 可用于后续机械改造后做 source feeder
  与工具头挤出机协同回抽；默认 `0`，不改变现有动作。
- 实机测试时必须先用短距离、低速、用户明确授权的动作验证方向和夹持能力，再扩大
  长度到四通外或完全退料。

## 背景问题

当前 source graph 已经能表达任意 source 到任意 head 的关系，但实机测试暴露出
执行层语义仍不够统一：

- native slot 曾被误当成 native toolhead，导致 source 状态和 head 状态互相猜测。
- `preload_finish` 曾被当作工具头已进料依据，但它只能表示 source 预进料完成。
- native 退料路径缺少 slot 侧回抽，工具头侧退料完成后，source 端未必真的退到
  四通外或完全退出。
- ACE 路径有 slot 侧回抽逻辑，native 路径没有等价抽象，导致两类 source 行为不一致。
- 前端曾把 source 操作和 toolhead 主流程混成可见队列，重复点击会制造重复动作和
  状态竞争。
- 打印途中 ACE slot 变为 empty 时，如果路径里仍有余料，不能直接当作普通 empty；
  这类状态应进入 exhausted/recovery 分支。

因此后续实现必须以“所有 source slot 具备同一套行为接口，具体硬件通过 adapter
实现”为基本架构。

## 核心原则

1. 工具头和耗材来源完全解耦。

   Toolhead 只负责 nozzle、extruder、head sensor、当前装载的 source。
   Source slot 只负责该耗材来源本身是否有料、料在线路中的位置、slot feeder 的动作。

2. 所有耗材入口都是 source slot。

   `native:<n>`、`ace:<ace>:<slot>` 以及后续新增外部送料器都属于同一层级。
   UI 文案必须显示为 0-based `Native Slot <n>`、`ACE <ace> Slot <slot>`，
   不要再用 `native T3` 这种容易和工具头混淆的名字，也不要再做 UI 1-based
   偏移。

3. `preload_finish` 不是 `load_finish`。

   `preload_finish` 只能表示 source slot 预进料完成，不能表示任何工具头已经装载。
   工具头是否装载必须由 `head.current_source + head.source_confidence` 表达。

4. Load / unload / swap 是 toolhead 主流程。

   用户要把某个 source 送进某个工具头时，应从 toolhead 侧发起主流程，后端协调
   source adapter 和 toolhead adapter。Source 卡片只暴露 source 自身动作，例如
   完全退料、耗材信息和执行参数。

5. 不再使用前端可见操作队列作为执行模型。

   后端必须提供单个 active operation lock。同一时刻只允许一个硬件动作运行；
   连续动作应封装成一个大宏/复合 operation，由后端按阶段执行和 post-check。

## 对象定义

### Source Slot

Source slot 是耗材来源，不是工具头。

职责：

- 记录该入口是否有料。
- 记录耗材是否已完成自动预进料。
- 记录耗材在 source 路径中的阶段，例如入口、预进料位、四通口外、正在推向工具头。
- 执行 feeder 侧推料、回抽、完全退料。
- 暴露材料、颜色、品牌、预设、执行参数。

不得负责：

- 判断工具头里当前装的是哪卷料。
- 单独把工具头标记为 loaded/empty。
- 在未绑定具体 head 的情况下执行进工具头动作。

建议 source runtime 字段：

```json
{
  "source_id": "native:3",
  "kind": "native_feeder",
  "presence": "present",
  "slot_state": "ready",
  "path_position": "preload_done",
  "last_error": null,
  "material": {
    "type": "PLA",
    "color": "#f0c040"
  }
}
```

建议 `slot_state`：

- `empty`：入口无料。
- `inserted`：检测到料，但尚未完成预进料。
- `preloading`：自动预进料中。
- `ready`：预进料完成，可参与 toolhead load。
- `pushing`：slot feeder 正在向四通或工具头方向推料。
- `retracting`：slot feeder 正在回抽到四通外。
- `ejecting`：slot feeder 正在完全退料到入口外。
- `exhausted`：slot 报 empty，但该 source 可能仍残留在路径或工具头内。
- `error`：source 动作失败，禁止自动继续。
- `unknown`：重启或状态缺失后无法确认。

建议 `path_position`：

- `not_present`
- `at_entry`
- `preload_done`
- `at_junction`
- `between_junction_and_head`
- `in_toolhead`
- `unknown`

`path_position=in_toolhead` 只是 source 侧对路径的辅助记录；最终工具头 loaded
状态仍以 toolhead runtime 为准。

### Toolhead

Toolhead 是物理打印头。

职责：

- 记录 head sensor 是否有料。
- 记录当前 loaded source。
- 执行工具头选择、挤出机进退、喷嘴加载确认、冲刷/prime。
- 发起 load、unload、swap 主流程，并协调 source slot。

不得负责：

- 把 native slot 或 ACE slot 当成自己的固定私有来源。
- 在 source 状态为 unknown/error/exhausted 时自动猜测来源。
- 用 source 的 `preload_finish` 推断自己 loaded。

建议 toolhead runtime 字段：

```json
{
  "head_id": "head:3",
  "index": 3,
  "sensor_present": true,
  "toolhead_state": "loaded",
  "current_source": "ace:0:2",
  "source_confidence": "known",
  "last_error": null
}
```

建议 `toolhead_state`：

- `empty`：传感器和运行态都确认无料。
- `loading`：正在执行 load/swap 进料阶段。
- `loaded`：已装载 `current_source`。
- `unloading`：正在执行退料阶段。
- `failed`：上次工具头动作失败，禁止自动继续。
- `unknown`：传感器有料但来源未知。
- `stale`：运行态记录有料，但传感器或 source 状态不一致。
- `exhausted`：当前 source 已断料，路径里可能仍有余料。

## 统一行为流程

下面的阶段是逻辑契约，不要求每个 adapter 都拆成独立 G-code，但执行结果和
post-check 必须等价。

### 1. Preload 自动预进料

触发：source slot 检测到用户插入耗材。

职责归属：source only。

行为：

- slot feeder 将耗材从入口送到预进料位置。
- 长度由 `source.execution.preload_length_mm` 控制。
- 完成后 source 进入 `slot_state=ready`、`path_position=preload_done`。

禁止：

- 不得更新任何 `head.current_source`。
- 不得把 toolhead 标记为 loaded。

### 2. Push To Junction 推料至四通口

触发：toolhead load/swap 主流程需要该 source。

职责归属：source adapter，由 toolhead operation 调用。

行为：

- source 从预进料位推到四通口或目标路径入口。
- 长度由 `source.execution.push_to_junction_length_mm` 控制。
- 完成后 source 可进入 `path_position=at_junction`。

注意：

- 如果实际硬件的预进料终点已经在四通口，该长度可以配置为 `0`。
- native 和 ACE 都必须具备同名参数，即使某些 adapter 内部不使用。

### 3. Load To Toolhead 推料接近工具头

触发：Push To Junction 成功后。

职责归属：source adapter + toolhead adapter 协同。

行为：

- 后端选择目标工具头。
- source feeder 按路径长度向目标 head 推料。
- 必要时工具头挤出机同步低速接料。
- 长度由 `source.execution.load_to_toolhead_length_mm` 控制。

完成条件：

- 工具头传感器开始检测到耗材，或 adapter 明确返回到达工具头入口。
- 未达到条件时不得继续 commit loaded。

### 4. Step Into Toolhead 步进进工具头

触发：料已接近工具头或工具头传感器触发。

职责归属：toolhead adapter 主导，source adapter 可 assist。

行为：

- 工具头挤出机执行进料、prime、flush 等动作。
- source feeder 可以按配置继续辅助推料。
- 长度由 toolhead/profile 参数控制，不应复用 preload 长度。

完成条件：

- 工具头 sensor 与 feeder 状态均无 error。
- 必要的挤出/冲刷流程完成。

### 5. Toolhead Loaded Commit

触发：Step Into Toolhead 成功后。

职责归属：后端状态层。

行为：

- 写入 `head.current_source = source_id`。
- 写入 `head.source_confidence = known`。
- source 可记录 `path_position=in_toolhead`，但不得作为唯一事实来源。

禁止：

- 任一步失败时不得提交 loaded。
- 不得在 source `empty/error/exhausted/unknown` 时提交 loaded。

### 6. Toolhead Unload 工具头退料

触发：用户从 toolhead 发起 unload，或 route plan 需要换到另一个 source。

职责归属：toolhead adapter + source adapter 协同。

行为：

- 工具头挤出机先把耗材从喷嘴/工具头内回抽出来。
- 同步或随后由 source feeder 回抽，避免耗材停留在四通内造成下一次进料堵塞。
- 该阶段不是 source card 上的“完全退料”；它只是把当前 source 从工具头退回
  到安全换料位置。

完成条件：

- 工具头 sensor 变为空，或 adapter 的可靠后验检查确认工具头已经空。
- source 已退到四通外或 adapter 定义的 safe park 位置。

### 7. Retract To Junction 回抽至四通外

触发：Toolhead Unload 过程的一部分。

职责归属：source adapter。

行为：

- slot feeder 按 `source.execution.unload_to_junction_length_mm` 回抽。
- 目标是让料头退出四通/混合路径，不阻塞其它 source 进料。

注意：

- ACE 当前有 slot 侧 retract 行为；native 必须补齐等价行为。
- 回抽方向必须先用短距离实机验证，确认 native channel 方向不反。

### 8. Toolhead Empty Commit

触发：Toolhead Unload + Retract To Junction 都成功后。

职责归属：后端状态层。

行为：

- 清除 `head.current_source`。
- 写入 `head.source_confidence = empty`。
- source 保持 `ready` 或 `at_junction/preload_done`，取决于 adapter 定义。

禁止：

- 工具头 sensor 仍有料时不得 commit empty。
- source 回抽失败时不得静默清空 `current_source`。

### 9. Full Unload Source 完全退料

触发：用户在 source slot 卡片点击“完全退料”。

职责归属：source only。

行为：

- slot feeder 将耗材从 source 路径完全退回入口外。
- 长度由 `source.execution.full_unload_length_mm` 控制。
- 完成后 source 进入 `slot_state=empty`、`path_position=not_present`。

前置条件：

- 该 source 不能是任何 toolhead 的 `current_source`。
- 该 source 不能处于 `pushing/retracting/loading/unloading`。
- 如果该 source 处于 `exhausted`，必须走 recovery 流程，而不是普通 full unload。

## Adapter 接口

每种 source 类型都必须实现同一套逻辑接口。函数名是架构契约，具体代码可按
Klipper/Web 实际结构落地。

```text
preload_source(source)
push_source_to_junction(source, head)
load_source_to_head(source, head)
step_into_toolhead(source, head)
unload_toolhead(source, head)
retract_source_to_junction(source, head)
full_unload_source(source)
recover_source(source, head, reason)
```

### Native Slot Adapter

必须支持：

- 自动预进料：检测插入后执行 `preload_length_mm`。
- push to junction：使用 native feeder 的 `module/channel` 推到四通。
- load to head：使用 source 自己的 `module/channel`，目标 head 只决定
  `EXTRUDER`。
- retract to junction：补齐 native feeder 反向回抽，不能只做工具头侧 unload。
- full unload：从当前 source path 完全退回入口外。

关键约束：

- `native:<n>` 不是 `head:<n>`。
- `native:<n>` 的 `module/channel` 来自 source，不来自目标 head。
- native slot 状态只能表达 source path，不表达工具头 loaded。

### ACE Slot Adapter

必须支持：

- ACE slot 预进料长度独立配置，`0` 表示禁用自动预进料。
- push/load/swap 时必须校验 source graph edge。
- unload 时必须同时完成工具头退料和 ACE slot 侧回抽。
- slot empty while loaded 必须进入 `exhausted`，不能按普通 empty 继续 swap。

关键约束：

- `ace:<ace>:<slot>` 只表达单个 ACE slot。
- 一台 ACE 不再整体归属于某个 head；每个 slot 通过 edge 独立连接 head。
- `head_source` 只能作为 ACE 路径的硬件事实之一，不能覆盖 native source state。

## Execution 参数

后端 schema 和前端 Config 页应逐 source 暴露这些参数：

```json
{
  "execution": {
    "preload_length_mm": 950,
    "push_to_junction_length_mm": 0,
    "load_to_toolhead_length_mm": 750,
    "unload_to_junction_length_mm": 120,
    "full_unload_length_mm": 950,
    "toolhead_sync_retract_length_mm": 0,
    "feed_speed_mm_s": 25,
    "retract_speed_mm_s": 25,
    "toolhead_sync_retract_speed_mm_s": 10
  }
}
```

字段语义：

- `preload_length_mm`：入口到预进料终点。
- `push_to_junction_length_mm`：预进料终点到四通/路径入口。
- `load_to_toolhead_length_mm`：四通/路径入口到工具头附近。
- `unload_to_junction_length_mm`：工具头退料时 source 侧回抽到四通外的距离。
- `full_unload_length_mm`：source 完全退回入口外的距离。
- `feed_speed_mm_s`：slot 侧进料速度。
- `retract_speed_mm_s`：slot 侧回抽速度。
- `toolhead_sync_retract_length_mm`：source 回抽期间工具头挤出机同步反抽长度。
  默认为 `0`，表示不执行同步反抽。
- `toolhead_sync_retract_speed_mm_s`：同步反抽速度。

默认值可以按 adapter 类型不同，但字段名必须统一。禁止再出现 ACE 有一套字段、
native 有另一套字段的情况。

## API 和操作边界

建议后端操作边界：

- `POST /api/operation/load`
  - 输入：`head_id`、`source_id`。
  - 执行：push to junction -> load to toolhead -> step into toolhead -> commit。
- `POST /api/operation/unload`
  - 输入：`head_id`。
  - 后端根据 `head.current_source` 找 source。
  - 执行：toolhead unload -> retract to junction -> commit empty。
- `POST /api/operation/swap`
  - 输入：`head_id`、`target_source_id`。
  - 执行：unload current source -> load target source。
- `POST /api/source/full-unload`
  - 输入：`source_id`。
  - 只执行 source full unload。
- `POST /api/operation/recover`
  - 输入：`head_id`/`source_id`、`reason`、用户确认。
  - 只处理 unknown/stale/failed/exhausted。

执行要求：

- 所有真实动作必须拿到全局 active operation lock。
- lock 持有期间普通 load/unload/swap/full-unload 请求返回 busy。
- 强制中断/取消必须是独立 recovery 入口，不能靠前端队列 clear 伪取消。
- 每个阶段完成后必须读取实时状态做 post-check。
- post-check 失败时进入 `failed`，不得继续执行后续阶段。

## UI 职责边界

### Source 卡片

显示：

- source id 和用户友好名称，例如 `Native Slot 3`、`ACE 0 Slot 1`。
- 材料、颜色、品牌、预设。
- source presence、slot_state、path_position。
- 可达 heads。
- execution 参数摘要。

动作：

- 编辑耗材信息。
- 编辑 source execution 参数。
- 完全退料。
- 进入 source recovery。

禁止：

- 不在 source 卡片上直接执行“进某个工具头”的完整 load。
- 不显示会让用户误以为该 source 等同于某个 toolhead 的文案。

### Toolhead 卡片

显示：

- head sensor。
- current_source。
- source_confidence。
- toolhead_state。
- 当前耗材信息。
- 可选 sources。

动作：

- Load：选择一个可用 source 后立即执行。
- Unload：不选择 source，后端使用 current_source。
- Swap：选择目标 source，后端自动 unload 当前 source 并 load 新 source。
- Recover：处理 unknown/stale/failed/exhausted。

禁止：

- Unload 时让用户选择 source。退料对象只能是当前 head 的 `current_source`。
- 用 `native T3` 这种文案混淆 native slot 和 toolhead T3。

## 安全不变量

必须满足：

- `preload_finish` 永远不更新 `head.current_source`。
- `load` 只有在 post-check 成功后才能提交 `head.current_source=source_id`。
- `unload` 只有在工具头空且 source 已退到安全位置后才能清除 `current_source`。
- `full unload` 禁止作用于任何 head 的 `current_source`。
- `unknown/stale/failed/exhausted` 禁止普通自动 load/unload/swap/print。
- `slot empty while loaded` 必须进入 `exhausted` 或 recovery，不得直接当作可用 empty。
- native 和 ACE adapter 都必须实现等价的 slot 侧回抽语义。
- `toolhead sensor cleared` 不等于 `path clear`，也不等于 `source/path safe`。
- `unload` 只有在工具头空、source/path 退到安全位置、transport/feed assist 已停止后
  才能清除 `current_source/head_source`。
- 任一 unload 结果不确定时必须阻止后续 load/swap，不能进入“先记成功再补救”的
  语义。
- route plan 打印前必须重新校验 source graph hash 和 source state。
- 任何实机方向、长度、速度参数变更，都必须先用短距离 dry-run/实机点动验证，
  禁止一上来执行长距离进退料。

## 开发落地顺序

1. 文档和命名收口。

   统一称 `native slot` / `ACE slot` / `toolhead`。文档、API、UI 禁止继续使用会
   混淆 source 与 head 的表达。

2. 后端状态模型收口。

   拆开 source runtime 与 head runtime。清理所有用 source 状态猜测 toolhead
   loaded、或用 toolhead 状态猜测 source ready 的逻辑。

3. Execution schema 扩展。

   在 source graph 中补齐 `push_to_junction_length_mm`、
   `load_to_toolhead_length_mm`、`unload_to_junction_length_mm`、
   `full_unload_length_mm`、速度参数。

4. Adapter 层实现。

   先实现 native slot adapter 的回抽和 full unload，再对齐 ACE slot adapter。

5. Operation 执行器重构。

   后端提供单 active operation lock，load/unload/swap 都按统一阶段执行，不依赖
   前端可见队列。

6. Dry-run mock 修正。

   mock 不能再把 `FEED_AUTO UNLOAD=1` 直接伪造成 head empty；必须模拟
   toolhead unload 和 source retract 两个阶段。

7. 前端操作重构。

   Source 只管 source，Toolhead 只管主进退料。Load 选择 source，Unload 不选择
   source。移除可见操作队列。

8. 实机短距离验证。

   先验证 native 各 channel 正反方向，再逐步验证 preload、push、load、unload、
   full unload。每一步只在用户明确允许后执行。

## 测试要求

Dry-run 必须覆盖：

- native slot preload 完成但 toolhead 仍为 empty。
- toolhead load 成功后才写入 current_source。
- toolhead load 失败时不写入 current_source。
- native unload 必须包含 toolhead unload + source retract。
- source retract 失败时不清空 current_source。
- source full unload 被 current_source 占用时拒绝。
- ACE slot empty while loaded 进入 exhausted。
- unknown/stale/failed/exhausted 阻止打印发送。
- 多 source 同 head swap 使用同一套阶段语义。
- swap/unload/load 失败、打印结束、手动中断后，transport/feed assist 必须进入明确
  stopped/disarmed 状态。
- `phase3 weak-success`、`wheel_delta=0/0`、`sensor_only` 等弱证据不能直接提交
  loaded，需要更强 post-check。

实机验证必须覆盖：

- 每个 native slot 短距离正向/反向确认。
- 每个 native slot 预进料长度可配置。
- native slot load 到指定 head。
- native slot unload 时能退出工具头并回抽到四通外。
- native slot full unload 能完全退到入口外。
- ACE slot 保持原有可用能力，并按统一状态提交。
- native + ACE 同 head swap 不再污染 `current_source`。

## 2026-06-16 问题复盘补充

本轮实机测试暴露出以下需要继续收口的点：

- 旧料未真正退出时，系统仍可能进入下一次 load。
- unload 的“完成”语义当前仍偏宽，需要分离：
  - 工具头传感器清空；
  - source/path 退到安全位置；
  - transport/feed assist 停止。
- 抖动现象只能记作待验证推测，不能在文档中定性为单一根因。
- 后续 dry-run 必须补一个“传感器已清空但路径未确认清空”的负例。

## 验收标准

达到可进入下一阶段代码重构的标准：

- 文档、API、UI 方案都明确区分 source slot 与 toolhead。
- 没有任何新逻辑把 `native:<n>` 等同于 `head:<n>`。
- preload、load、unload、full unload 四个词在文档中语义固定。
- 后续代码改动可以按 adapter 接口逐项验收。

达到实机可测的标准：

- 后端 dry-run 覆盖所有安全不变量。
- source-state 能同时展示 source slot 状态和 toolhead loaded 状态。
- native unload 已补齐 source 侧回抽。
- 前端不再提供 unload 选 source 的错误入口。
- 任一阶段失败时不会把未进料状态标记成 loaded，也不会把未退净状态标记成 empty。
