# native/ACE 协同 MVP 开发规划

日期：2026-06-05
当前基线：`82d26f4 Prune stale multiACE web alerts`
当前状态：native/ACE 混合打印 MVP 已通过实机测试

## 目标判断

单头单 ACE MVP 已经证明核心链路成立：一个 U1 物理工具头可以由
ACE 接管，并通过显式命令完成 4 色打印：

```gcode
ACE_SWAP_HEAD HEAD=<ace_head> ACE=<ace_index> SLOT=<slot_index>
```

接下来不应继续把换色效率作为 MVP 阶段的主要开发目标。换色慢、
退料保守、purge 策略粗糙，都属于后续体验和效率优化。当前更重要的
MVP 缺口是 native 工具头与 ACE 工具头能否在同一份打印任务中协同。

因此路线调整为：

1. 冻结单头单 ACE MVP，只修阻断真实打印或损坏状态的 blocker。
2. 推进一个 ACE head + 若干 native heads 的协同打印 MVP。
3. 协同能力落地后，再统一优化换色效率、purge、排布策略和 UI 体验。

## 当前状态

### 已具备能力

- Dashboard 可配置工具头为 `native` 或 `ace`。
- ACE head 可绑定到 ACE 设备。
- ACE slot 可配置材料、品牌、子类型、颜色。
- 单头单 ACE 模式下，preflight 会把 slicer 虚拟工具重写到显式
  `ACE_SWAP_HEAD HEAD=<head> ACE=<ace> SLOT=<slot>`。
- 后端拒绝隐式 ACE/slot 猜测，`ACE_LOAD_HEAD` 和 `ACE_SWAP_HEAD`
  都要求显式 `HEAD`、`ACE`、`SLOT`。
- `head_source` 已用于记录每个工具头当前来自哪个 ACE/slot。
- 已修复旧 `gate_status` 导致的 `slot_empty (post-unload)` 误判。
- 已修复 Web alert 跨打印会话残留的问题。
- 实机已经完成单头 ACE 多色打印的基础验证。

### 仍需严守的 blocker

以下问题一旦出现，需要优先修复，不能进入优化阶段：

- 进错 ACE slot。
- ACE 逻辑误操作 native head。
- `head_source` 与真实物理状态不一致。
- load/unload 失败后仍被标记为成功。
- Klipper halt 或打印不可恢复。
- Web preflight 映射与后端实际命令不一致。
- 断电/重启后恢复状态会误导下一次 load/unload。

### 暂缓优化项

以下问题记录为后续优化，不阻塞 native/ACE 协同 MVP：

- 换色耗时长。
- `swap_retract_length` 偏保守。
- purge 量偏大。
- 未做颜色/材料最优排布。
- 未做最少换色次数策略。
- 未做 native/ACE 混合时的高级 wipe/purge tower 策略。
- UI 操作流仍可继续精简。
- Web 视觉细节和告警体验可继续打磨。

## native/ACE 协同 MVP 范围

### 支持范围

- 一台 ACE。
- 一个工具头配置为 ACE head。
- 其余工具头可配置为 native head。
- 一份 G-code 内允许 native head 和 ACE head 交替参与打印。
- ACE head 的所有换料都使用显式命令：

```gcode
ACE_SWAP_HEAD HEAD=<ace_head> ACE=0 SLOT=<slot>
```

- native head 继续使用普通工具切换：

```gcode
T<n>
```

### 暂不支持

- 多个 ACE head。
- 多台 ACE 参与同一任务。
- 一个 ACE 动态绑定多个 head。
- 自动优化颜色到 head/slot 的全局排布。
- 跨 ACE feed assist 优化。
- 自动判断哪个物理头接了 ACE。
- 自动从硬件反推耗材接线。

## 核心模型

协同 MVP 需要把 slicer 的虚拟工具解析成统一的 print source：

```text
slicer tool -> print source

print source:
  native:
    head: <0..3>

  ace:
    head: <0..3>
    ace:  <0>
    slot: <0..3>
```

示例：

```text
Slicer T0 -> Native Slot 0 -> T0
Slicer T1 -> ACE0 Slot2 -> physical T3
Slicer T2 -> Native Slot 1 -> T1
Slicer T3 -> ACE0 Slot0 -> physical T3
```

这个 resolver 是下一阶段的核心。所有 UI、preflight、G-code rewrite、
安全校验都应该围绕这个结果展开。

## 后端开发方案

### 1. 路由配置读取

继续使用现有配置字段：

- `head0_mode` ... `head3_mode`
- `ace0_head`
- 后续保留 `ace1_head` ...，但 MVP 只启用 ACE0

要求：

- 配置为 `native` 的 head 不能执行 ACE load/unload/swap。
- 配置为 `ace` 的 head 必须被某个 ACE target 引用。
- MVP 阶段最多允许一个 ACE head。
- 如果没有 ACE head，则退化为 native-only。

### 2. preflight resolver

新增或重构一个解析阶段：

1. 读取 slicer tools、颜色、材料。
2. 读取 live native head 耗材配置。
3. 读取 live ACE slot 耗材配置。
4. 为每个 slicer tool 生成明确 target：
   - native head
   - 或 ACE slot
5. 任何无法明确映射的 tool 都阻止发送打印。

第一版匹配策略保持简单：

- 优先 exact material + color。
- 其次 exact color。
- 最后允许用户在 UI 中手动改映射。
- 不做自动优化。

### 3. G-code rewrite

当前单头 ACE rewrite 假设所有 slicer tool 都进入 ACE head。协同 MVP
需要改成按 resolver 输出分别处理：

- 目标是 native：

```gcode
T<n>
```

- 目标是 ACE slot：

```gcode
ACE_SWAP_HEAD HEAD=<ace_head> ACE=0 SLOT=<slot>
```

必须保留的安全规则：

- 不能把 native target 重写成 ACE 命令。
- 不能把 ACE target 重写成 native `T<n>` 后跳过换料。
- 每个 ACE 命令都必须包含完整 `HEAD`、`ACE`、`SLOT`。
- 生成文件头部应写入 resolver summary，方便追查。

### 4. 打印开始前校验

发送前必须校验：

- 当前配置仍与 preflight 生成时一致。
- ACE 设备在线。
- ACE head 与 ACE target 一致。
- 所有 ACE slot 均为 ready 或 feeding-compatible 状态。
- native head 的耗材配置未缺失。
- `head_source` 没有与目标打印起点冲突。

如果校验失败，禁止发送打印。

## Klipper 侧开发方案

### 1. 保持显式命令边界

继续禁止隐式 ACE/slot 推断：

```gcode
ACE_LOAD_HEAD HEAD=<head> ACE=<ace> SLOT=<slot>
ACE_SWAP_HEAD HEAD=<head> ACE=<ace> SLOT=<slot>
ACE_UNLOAD_HEAD HEAD=<head>
```

### 2. 强化 head mode 校验

当前 `_check_routed_head` 是安全边界。native/ACE 协同 MVP 中它必须满足：

- `native` head 拒绝 ACE load/swap/unload。
- `ace` head 只允许访问绑定到它的 ACE。
- 如果 ACE target 配置为空，ACE 命令直接拒绝。
- 如果配置里出现多个 ACE head，MVP 阶段直接拒绝启动或拒绝 preflight。

### 3. native 工具头保护

native head 的耗材状态只用于 UI 和 preflight，不写入 ACE `head_source`。

禁止行为：

- native head 被 `ACE_UNLOAD_ALL_HEADS` 清掉真实耗材信息。
- ACE recovery 逻辑猜测 native head 的来源。
- native head 的传感器状态被当成 ACE load 结果。

### 4. 换头和换料顺序

初版不优化，只保证正确：

- native -> native：普通 `Tn`。
- native -> ACE：切到 ACE physical head，然后执行 `ACE_SWAP_HEAD`。
- ACE -> native：先完成 ACE swap/状态恢复，再执行 native `Tn`。
- ACE slot A -> ACE slot B：执行 `ACE_SWAP_HEAD`。

## Web UI 开发方案

### Dashboard

Dashboard 继续作为工具头模式配置主入口：

- 每个工具头显示 mode selector：`native` / `ace`。
- native head 二级配置：材料、品牌、子类型、颜色。
- ace head 二级配置：绑定 ACE。
- ACE 二级配置：4 个 slot 的材料、品牌、子类型、颜色。

要求：

- 修改配置后只进入 pending 状态。
- 用户点击提交按钮后才写配置并提示需要 restart。
- 不允许改一下就自动重启 Klipper。

### Preflight

新增协同映射表：

```text
Slicer Tool | Material | Color | Target
T0          | PLA      | red   | Native Slot 0 -> T0
T1          | PETG     | blue  | ACE0 Slot2 -> T3
```

第一版必须支持手动修改每个 slicer tool 的 target。

发送前展示最终命令意图：

```text
T0 -> Native Slot 0 -> T0
T1 -> ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=2
```

如果任何一行无法解析，禁用发送按钮。

## Docker dry-run 方案

native/ACE 协同应先在 docker dry-run 验证 UI 和 rewrite，不接硬件。

需要扩展 mock 状态：

- 4 个 toolheads。
- 1 个 ACE。
- 至少一个 head mode 为 `ace`。
- 至少一个 head mode 为 `native`。
- mock `head_source`。
- mock native filament metadata。

dry-run 验收：

- 配置工具头模式不会自动重启。
- preflight 能生成 mixed resolver summary。
- rewrite 后 G-code 同时包含 native `Tn` 和 `ACE_SWAP_HEAD`。
- 无法映射时阻止发送。
- 刷新页面后配置状态一致。

### 实现进度：2026-06-05

已完成第一轮 dry-run 可验证实现：

- 后端新增 mixed resolver，输出 `slicer tool -> native head / ACE slot`
  的 `tool_targets`。
- preflight 返回 mixed mapping，并保存同一份 `tool_targets` 供发送打印
  时复用。
- postprocess rewrite 支持 mixed targets：
  - native target 输出普通 `T<head>`。
  - ACE target 输出 `T<head>` + `ACE_SWAP_HEAD HEAD=<head> ACE=<ace> SLOT=<slot>`。
- Web preflight 表格能显示 `Native Slot n -> Tn` 和 `ACE n Slot m -> Tn`。
- docker dry-run mock 增加一个有料 native head，便于无硬件验证。

dry-run 结果：

```text
Slicer T0 -> Native Slot 1 -> T1
Slicer T1 -> ACE0 Slot1 -> T0
```

最终上传到 mock Moonraker 的 G-code 同时包含 native `T1` 和
`ACE_SWAP_HEAD HEAD=0 ACE=0 SLOT=1`，证明 preflight mapping 与
实际 rewrite 输出一致。

后续在该链路上继续做效率和体验优化，不再把 native/ACE 协同本身视为
未完成能力。

## 实机验证方案

### 验证前置

- ACE0 4 槽接到一个物理工具头，例如 T3。
- 至少一个其他工具头保持 native。
- native head 中实际装入一卷与 UI 配置一致的耗材。
- G-code 使用至少一个 native 工具和至少一个 ACE slot。

### 第一轮实机测试

目标：只验证协同切换是否可用，不验证效率。

建议测试模型：

- 两色或三色小模型。
- 一种颜色走 native。
- 一种或两种颜色走 ACE。
- 层数少，换色次数少。

记录：

- preflight resolver summary。
- 实际生成的 toolchange 命令。
- 每次 native/ACE 切换的 Klippy log。
- `head_source` 变化。
- `print_stats.exception`。
- 是否出现 pause。

通过标准：

- native head 不被 ACE load/unload。
- ACE head 每次进入正确 slot。
- 打印能完成。
- Web UI 和实际打印状态一致。
- 出错时 recovery 信息明确，不污染下一次测试状态。

## 建议开发顺序

1. 后端 resolver 数据结构。
2. G-code rewrite 支持 mixed targets。
3. Web preflight 显示 mixed mapping。
4. Web preflight 手动 target override。
5. Docker dry-run mock 状态扩展。
6. docker 验证配置和 rewrite。
7. Klipper 侧补强 `_check_routed_head` 和 native 保护。
8. 实机部署。
9. 小模型 native/ACE 协同打印。
10. 只修 blocker，不做效率优化。

## MVP 完成标准

native/ACE 协同 MVP 完成，不等于高效。完成标准是：

- 用户能明确配置哪个 head 是 native，哪个 head 是 ACE。
- 用户能明确配置 native head 和 ACE slot 的耗材。
- preflight 能把 slicer tools 映射到 native head 或 ACE slot。
- 生成 G-code 与 UI 映射一致。
- 实机能完成至少一份 native + ACE 混合打印。
- 错误不会造成不可逆硬件风险。
- 错误恢复不会污染 `head_source` 或下一轮打印。

换色效率、排布优化、purge 策略和 UI polish 在该标准之后再进入下一阶段。

## 实机验证结果：2026-06-05

### 已验证拓扑

```text
T0: native
T1: native
T2: native
T3: ACE head
ACE0 -> T3
```

实机预检曾返回如下 mixed mapping：

```text
Slicer T0 -> Native Slot 0 -> T0
Slicer T1 -> ACE0 Slot1 -> T3
Slicer T2 -> Native Slot 1 -> T1
Slicer T3 -> Native Slot 2 -> T2
```

实际打印测试结果：

- native + ACE 多头协同打印可以正常开始并完成核心切换链路。
- native heads 不再被 ACE load/unload 逻辑接管。
- ACE head 的 `head_source` 能保持为真实 ACE/slot。
- preflight UI 显示的映射与后端发送打印使用的 `tool_targets` 一致。
- 手动 override 已可用；重复目标会在前端禁用，并在后端发送前拒绝。

### 本轮修复的关键问题

- mixed resolver 支持把 slicer tool 映射到 native head 或 ACE slot。
- postprocess rewrite 支持 mixed targets：
  - native target -> 普通 `T<head>`。
  - ACE target -> `T<head>` + `ACE_SWAP_HEAD HEAD=<head> ACE=<ace> SLOT=<slot>`。
- `Printer loadout (live)` 同时显示 native heads 和 ACE slots。
- native 匹配加入颜色距离，避免只按材料默认映射。
- preflight 发送打印时持久化并校验 `tool_targets`。
- `FEED_AUTO` 参数增加 Web 后端校验，降低错误通道/错误 head 风险。
- Klipper ghost-head 检测改为只检查 ACE mode head，避免把正常 loaded
  native head 误报为缺失 `head_source`。

### 当前保留限制

- MVP 只证明“能正确混合打印”，不代表效率已经可接受。
- 当前自动映射仍是保守贪心匹配，不做全局换色次数最优。
- ACE slot 仍以 ACE 设备为单位绑定到 ACE head，暂不支持任意 slot 到任意
  toolhead 的复杂路由。
- native head 目前是“一头一卷”的基础模型，尚未支持单 native head 外接多个
  普通进料器做换料。
- purge、wipe、退料长度、温度等待等策略仍偏保守。
- UI 已可测试，但还不是最终的生产级工作流。

## 下一阶段 TODO

### A. 算法和打印效率

1. 降低无意义换色：
   - 统计实际 toolchange 序列。
   - 在不改变物理能力边界的前提下，重新排序 slicer tool -> target 映射。
   - 优先让高频颜色落到 native head 或当前已加载的 ACE slot。

2. 优化 ACE swap 策略：
   - 识别连续使用同一 ACE slot 的场景，跳过重复 swap。
   - 记录 ACE head 当前 slot，减少不必要 unload/load。
   - 将 `head_source` 与 preflight 初始状态纳入 swap 计划。

3. 优化退料/进料参数：
   - 单独记录 unload 到四通、退到 ACE、到喷嘴三段长度。
   - 针对硬料/软料设置不同 retry 和 wiggle 策略。
   - 用实际失败日志反推更合理的默认 `swap_retract_length`。

4. purge/wipe 策略：
   - 按材料和颜色差动态估算 purge 量。
   - 区分同材质近色、同材质远色、跨材质三类。
   - 后续考虑 purge tower 或对象内 purge。

### B. UI 和交互逻辑

1. preflight 页面：
   - 在发送前展示最终命令意图，例如 `T3 + ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=1`。
   - 对低置信度匹配给出醒目标识，但允许手动确认。
   - 增加“一键按当前实机 loadout 自动重算映射”。

2. Dashboard：
   - native 和 ACE 卡片显示风格继续统一。
   - 工具头模式、ACE 归属、耗材配置继续保持 staged/apply 模式。
   - 增加打印中只读/禁止危险修改的 UI 状态。

3. 错误恢复：
   - 将 load/unload/swap 错误按 head 和 source 分组显示。
   - 区分“可继续打印的警告”和“必须停止的硬错误”。
   - recovery 按钮只暴露与当前 head/source 匹配的安全动作。

### C. 扩展能力

1. 单 native head + 多普通进料器：
   - 建立 `native source` 抽象，支持一个 head 对应多个普通进料通道。
   - 设计普通进料器的 load/unload/swap 命令边界。
   - 复用 mixed resolver，让 target 不只包含 `native head`，而是包含
     `native feeder -> head`。

2. ACE slot 任意映射：
   - 从 `aceN_head` 设备级绑定升级到 slot/source 级路由。
   - 支持 `ACE0 Slot0 -> T0`、`ACE0 Slot1 -> T3` 这类复杂接线。
   - 每个 slot 必须有明确 target head，禁止硬件自动猜测。

3. 多 ACE / 多 ACE head：
   - 扩展 resolver 到多个 ACE 设备。
   - 支持多个 ACE head 同时存在。
   - 增加跨 ACE 的 slot/head 冲突校验。

### D. 安全和回归

1. 为以下场景建立固定 dry-run 测试：
   - native-only。
   - single ACE head。
   - native + one ACE head。
   - unmapped slicer tool。
   - duplicate manual target。
   - wrong FEED_AUTO channel。

2. 实机回归 checklist：
   - 进料目标是否正确。
   - 退料是否真正退出四通。
   - `head_source` 是否与实际 slot 一致。
   - native head 是否没有被 ACE recovery 清掉。
   - 断电/重启后状态是否不误导下一次打印。

3. 日志和诊断：
   - 每次 preflight 保存 resolver summary。
   - 每次发送打印保存 rewritten gcode 的 source map。
   - Klipper log 中对 mixed target 输出明确 head/source。
