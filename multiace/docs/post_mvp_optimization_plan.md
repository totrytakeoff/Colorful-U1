# Colorful-U1 后 MVP 优化路线

日期：2026-06-05

当前基线：

- 单头单 ACE 4 色打印链路已完成基础验证。
- native + ACE 多头混合打印已通过实机测试。
- 当前项目重点从“证明能正确混合打印”转入“稳定化、效率优化、工作流集成”。

## 阶段划分

后续开发分为四个阶段：

```text
Phase 1: 稳定化和可观测性
Phase 2: 低风险效率优化
Phase 3: 高级路由能力
Phase 4: 切片软件集成
```

切片软件集成不建议立刻做成主线任务。它应该放在 Phase 4，但 Phase 1
就要开始为它准备稳定 API、source map、dry-run 测试和打印发送协议。

原因很简单：如果 Web preflight、映射结果、后端重写和错误恢复还没有稳定，
直接集成进切片软件只会把同一批不稳定逻辑复制到另一个入口里，后续维护成本
会很高。

## Phase 1：稳定化和可观测性

目标：让当前已经能打印的 native/ACE 混合链路变成可复现、可排查、可回归的
稳定基线。

### 实现进度：2026-06-05

已落地第一轮收尾：

- Web 后端为每次 preflight 生成并持久化 `source_map`。
- `source_map` 记录 slicer tool、材料、颜色、最终 target、命令预览和
  当前 topology 快照。
- 新增 `GET /api/preflight/source-map?token=<token>`，方便 Web UI、
  dry-run 测试和后续切片软件集成复用同一份映射结果。
- preflight 发送打印时会重新校验手动映射，并用最终 target 覆盖保存
  `source_map`。
- Web preflight 弹窗新增最终命令预览，能直接看到 native `Tn` 和
  `ACE_SWAP_HEAD HEAD=<head> ACE=<ace> SLOT=<slot>`。
- 打印机处于 `printing`、`paused` 或 `busy` 时，Dashboard 锁定工具头
  topology 修改和提交。
- Docker dry-run 增加 `regression_preflight.py`，覆盖 mixed preflight、
  source map、发送打印和最终上传 G-code 校验。

### 必做项

1. 固化 dry-run 回归测试：
   - native-only。
   - single ACE head。
   - native + one ACE head。
   - unmapped slicer tool。
   - duplicate manual target。
   - wrong `FEED_AUTO` channel。
   - stale `head_source`。

2. 保存 preflight source map：
   - 每个 slicer tool 的材料、颜色、原始 T 编号。
   - resolver 输出的最终 target。
   - 最终生成的 G-code 命令意图。
   - 当前 toolhead/ACE/slot 配置快照。

3. 打印发送前展示最终命令意图：

   ```text
   Slicer T0 -> Native T0
   Slicer T1 -> T3 + ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=1
   ```

4. 打印中锁定危险配置：
   - 工具头 `native` / `ace` 模式。
   - ACE 归属。
   - ACE slot 到 head 的物理路由。
   - 正在参与当前任务的耗材映射。

5. 错误分级：
   - warning：不影响当前打印继续。
   - recoverable pause：需要用户处理后可 resume。
   - hard blocker：禁止继续打印或禁止发送任务。

### 阶段完成标准

- 同一份测试 G-code 在 dry-run 中能稳定生成一致 source map。
- Web UI 显示的映射、后端保存的映射、最终上传的 G-code 三者一致。
- 出错时能从日志里还原：哪个 slicer tool、哪个 head、哪个 ACE、哪个 slot。
- 打印中不会通过 UI 改坏路由配置。

## Phase 2：低风险效率优化

目标：先减少不必要的换料次数，再优化单次换料耗时。优先做不改变硬件控制
边界的优化。

### 2.1 减少换料次数

这是收益最大、风险最低的方向。

可做项：

- 统计整份 G-code 的 toolchange 序列。
- 统计每个 slicer tool 的出现次数、连续段数量和层分布。
- 识别连续目标相同的 ACE slot，跳过重复 `ACE_SWAP_HEAD`。
- 优先把高频颜色分配给 native head。
- 优先复用当前已经 loaded 的 ACE slot。
- 如果某层颜色数不超过可用物理 source 数，尽量减少层内换料。

第一版不要追求全局最优，只需要做到：

```text
当前映射：预计 34 次 ACE swap
优化映射：预计 18 次 ACE swap
```

能稳定减少明显无意义 swap 即可。

### 2.2 优化映射策略

当前 resolver 的目标是正确，不是高效。后续可以在 preflight 中提供几种模式：

```text
Safe: 按当前物理 loadout 保守匹配
Fewer swaps: 根据 toolchange 频率减少 ACE swap
Manual: 用户完全手动指定
```

初期策略：

- 材料必须匹配优先。
- 色差越小置信度越高。
- 高频颜色优先 native。
- 高频交替的两个颜色尽量不要都放在 ACE 上。
- 点缀色可以放 ACE slot。
- 当前已经 loaded 的 ACE slot 获得额外权重。

### 2.3 跳过无效 swap

需要明确区分三种状态：

```text
target source: 本次目标 source
known source:  软件记录的当前 head_source
physical source: 用户确认的真实物理状态
```

可直接跳过的场景：

- `known source == target source`。
- preflight 初始状态明确确认 ACE head 已经 loaded 到目标 slot。

不可跳过的场景：

- `head_source` 缺失。
- 上一次 load/unload 失败。
- 用户手动干预过料路但未确认。
- 传感器状态与 `head_source` 冲突。

### 2.4 换料耗时统计

在开始改换料参数前，必须先记录耗时分解：

```text
swap total: 168s
unload: 53s
load-to-sensor: 62s
extrusion-check: 18s
purge/prime: 35s
```

每次 swap 至少记录：

- head。
- ACE。
- slot。
- material。
- color。
- unload 耗时。
- load 耗时。
- purge/prime 耗时。
- retry 次数。
- 是否触发 pause。

没有这些数据，不建议盲目调速度和长度。

## Phase 2.5：单次 ACE swap 优化

这部分直接影响硬件动作，必须在 Phase 2 的统计能力完成后再做。

### 可优化方向

1. 退料长度分段：

   ```text
   nozzle -> toolhead sensor
   sensor -> 四通外
   四通外 -> ACE 内部
   ```

   目标是避免每次都用最保守的大回抽。

2. 按材料配置参数：
   - PLA/PETG 可以逐步提高速度。
   - TPU/软料保持保守。
   - 易脆材料降低速度和拉扯。

3. 区分 load/unload/feed-assist 策略：
   - print-time feed assist。
   - load-time feed assist。
   - unload-time feed assist。

4. 减少不必要等待：
   - sensor wait。
   - retry 间隔。
   - 重复 wheel check。

5. purge/prime 动态化：
   - 同材质近色少 purge。
   - 同材质远色中等 purge。
   - 深色到浅色更多 purge。
   - 跨材质保守 purge。

### 禁止事项

- 不允许为了速度跳过 load 结果校验。
- 不允许 load 失败后仍标记完成。
- 不允许在 `head_source` 不可信时跳过 unload/load。
- 不允许把 native head 纳入 ACE recovery 猜测。

## Phase 3：高级路由能力

目标：扩展硬件路由模型，但仍保持显式配置和可验证 source map。

### 3.1 ACE slot 任意映射

从当前设备级绑定：

```text
ACE0 -> T3
```

升级为 slot/source 级绑定：

```text
ACE0 Slot0 -> T3
ACE0 Slot1 -> T3
ACE0 Slot2 -> T1
ACE0 Slot3 -> T2
```

要求：

- 每个 slot 必须有明确 target head。
- 不允许通过硬件自动猜测 slot 接到了哪个 head。
- preflight source map 必须保存 slot/head 快照。
- Klipper 侧必须拒绝配置外的 slot/head 组合。

### 3.2 单 native head + 多普通进料器

引入 `native source` 抽象：

```text
native feeder/source -> head
```

resolver 不再只输出 `Native Tn`，而是输出：

```text
NativeSource N -> Tn
```

这个能力可以复用 ACE mixed resolver，但需要新的 native load/unload/swap
命令边界。

### 3.3 多 ACE / 多 ACE head

在 slot 任意映射稳定后，再扩展：

- 多台 ACE。
- 多个 ACE head。
- 跨 ACE 冲突校验。
- 多 ACE source map。

## Phase 4：切片软件集成

目标：用户不再需要手动导出 G-code 再上传 Web UI，而是在切片软件中直接完成
耗材映射、ACE 管理、preflight 和发送打印。

这个阶段不应该替代 Web UI，而应该复用 Web 后端能力。Web UI 仍作为打印机端
事实来源，切片软件只作为更方便的客户端入口。

### 为什么放在 Phase 4

切片软件集成依赖以下前置能力：

- 稳定的 preflight API。
- 稳定的 source map 格式。
- 可复现的 G-code rewrite。
- 清晰的错误码和错误分级。
- 可查询的 printer loadout。
- 可查询和可设置的 native/ACE 耗材配置。
- dry-run 回归测试。

如果这些没有稳定，切片集成会变成另一个复杂 UI 和发送入口，问题更难定位。

### Phase 1 就要预留的接口

虽然正式集成放在 Phase 4，但 Phase 1 开始就应该按“外部客户端也会调用”的
方式设计 API：

```text
GET  /api/state
GET  /api/loadout
POST /api/preflight/analyze
POST /api/preflight/resolve
POST /api/preflight/send
GET  /api/preflight/<id>
GET  /api/materials
POST /api/native-override
POST /api/slot-override
```

后续可以把现在 Web UI 内部使用的接口收敛成稳定协议。

### 可能的集成形态

1. 切片器后处理脚本：
   - 最容易落地。
   - 适合早期验证。
   - 可以在导出后自动调用 Colorful-U1 preflight API。
   - 仍然可能需要浏览器确认映射。

2. OrcaSlicer / PrusaSlicer 配置模板：
   - 通过 printer profile、custom G-code、post-processing script 集成。
   - 维护成本中等。
   - 适合先支持高级用户。

3. 切片器插件或外部 companion app：
   - 用户体验最好。
   - 开发和维护成本最高。
   - 需要处理切片器版本差异、认证、网络发现、错误 UI。

4. Moonraker/Fluidd 风格直连发送：
   - 可以让切片器直接发送到 Colorful-U1 后端。
   - Colorful-U1 后端负责 preflight、rewrite、上传 Moonraker。
   - 推荐作为长期方向。

### 推荐路线

```text
Phase 1:
  稳定 API、source map、dry-run。

Phase 2:
  preflight 输出预计 swap 次数、预计耗时、优化建议。

Phase 3:
  source map 支持复杂路由。

Phase 4a:
  提供 slicer post-processing script，自动调用 preflight API。

Phase 4b:
  提供 Orca/Prusa 配置模板和使用文档。

Phase 4c:
  再考虑完整插件或 companion app。
```

### 切片软件集成的边界原则

- 切片器不能绕过 Colorful-U1 后端直接生成危险 ACE 命令。
- 最终发送前仍必须经过 Colorful-U1 resolver 校验。
- 打印机当前 loadout 必须来自打印机端真实状态。
- 用户在切片器中配置的耗材映射必须能回写到打印机端。
- 出错时以打印机端 source map 和日志为准。

## 建议近期开发顺序

1. Phase 1：source map 持久化。
2. Phase 1：最终命令意图预览。
3. Phase 1：dry-run 回归脚本。
4. Phase 1：打印中锁定危险配置。
5. Phase 2：统计 swap 次数和耗时。
6. Phase 2：跳过重复 swap。
7. Phase 2：preflight 显示预计换料次数。
8. Phase 2：低风险映射优化。
9. Phase 4a 原型：post-processing script 调用 Colorful-U1 API。

切片软件完整集成可以开始调研，但不建议早于 Phase 2 完成前进入主线实现。
