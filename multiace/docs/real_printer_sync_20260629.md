# 2026-06-29 实机同步与 T0 stale 状态修复记录

本文记录 `source-graph-architecture` 分支在 2026-06-29 从实机
`192.168.1.38` 同步回仓库的逻辑，以及随后针对 T0 stale/head-source 残留问题的
修复。实机备份目录为本地排除项，不进入 Git。

## 背景

T0 曾在人工拔出耗材后出现软件状态残留：

- ACE slot 已插入料，但尚未进入 T0。
- T0 传感器实际无料。
- 旧 `head_source` / Web 缓存仍记录 T0 绑定某个 source。
- `/api/source-state` 因旧映射显示 `source_confidence=stale`，Web UI 的普通
  load/print/recover 流程都会被运行态校验拦截。

核心问题不是单个前端按钮，而是运行态事实来源没有在“空闲且传感器确认无料”时主动
清理 stale head-source 映射。

## 从实机同步回仓库的内容

本轮先把仓库中已有、实机上也存在的相关文件拉回对比，确认实机逻辑相对仓库已有
若干前序修改。同步原则：

- 只同步仓库内对应文件已有的逻辑和配置。
- 保留本地 `printer-backups/` 作为分析备份，不提交。
- 不覆盖无关文件，不回滚用户已有改动。

同步后的主要文件：

- `multiace/klipper/extras/ace.py`
- `multiace/web/backend/main.py`
- `multiace/web/frontend/app.js`
- `multiace/web/frontend/index.html`
- `multiace/docker-dryrun/mock_moonraker.py`
- `multiace/docker-dryrun/regression_preflight.py`

## 修复设计

### Klipper 侧

`multiace/klipper/extras/ace.py` 新增空闲同步清理：

- 新增 `_prune_stale_head_sources_if_idle()`。
- 在 `get_status()` 中节流调用。
- 当满足以下条件时清除对应 `self._head_source[head]`：
  - 当前没有 swap/internal load。
  - 非打印中的自动送料上下文。
  - head 仍有旧 source 映射。
  - 对应工具头传感器确认无料。
- 清理后保存 `head_source`、清空显示材料，并写 audit 日志。

这使实机在人工拔料或异常中断后，能按传感器事实自动收敛，不长期保留旧映射。

### Web 后端

`multiace/web/backend/main.py` 做了两层保护：

- `_head_source_records_for_state()` 读取 Web 统一运行态缓存时，如果工具头传感器为空，
  直接忽略该 head 的旧缓存映射，避免 `head_source_state.json` 把空头重新解释成
  stale/loaded。
- 新增 `POST /api/operation/head/recover`，仅允许处理
  `source_confidence=stale` 且工具头传感器无料的 head。

recover operation 生成 `ACE_CLEAR_HEADS HEAD=<n>`，执行后做 post-check：

- 工具头传感器仍必须无料。
- source 传感器不能显示该 head 仍有料。
- Web 统一运行态清除该 head 映射。
- 最终 `/api/source-state` 必须回到 `source_confidence=empty`。

route plan 校验同步允许 `recover` 这种没有 source edge 的硬件恢复事件，但仍只限
operation 模式，普通打印 route plan 不因此放宽。

### Web 前端

`multiace/web/frontend/app.js` 和 `index.html` 调整恢复按钮行为：

- `stale + current_source` 调用 `/operation/head/recover`。
- `exhausted + current_source` 仍走退料恢复。
- 其他异常状态只提示用户检查物理料路，不再把所有恢复都错误地路由到 unload。

这解决了 T0 已经实际无料时，Web UI 仍要求退料而退料又无法通过状态校验的问题。

### Dry-run 回归

dry-run mock 新增 `ACE_CLEAR_HEADS` 支持，并新增
`test_stale_head_recover_clears_mapping`：

- 构造 head 有旧映射、传感器为空的 stale 场景。
- 确认 `/source-state` 先识别为 `stale`。
- 确认 recover 预览生成 `ACE_CLEAR_HEADS HEAD=0`。

## 实机验证结果

本轮部署到实机后验证：

- Klipper 重启后 ready。
- Web `/api/health` 返回 200。
- 新增 `/api/operation/head/recover` 已被实机后端加载。
- T0 最终状态恢复为：
  - `current_source: null`
  - `source_confidence: empty`
  - `sensor_filament: false`

同时手动备份并移除了实机旧
`/home/lava/printer_data/config/extended/multiace/head_source_state.json` 中的
`head:0` stale 缓存项，以便立即恢复当前机器状态。根因修复仍在代码层：后续同类
缓存残留会被传感器空状态过滤，并可通过 recover operation 清理。

## 磁盘清理说明

部署后 Web 曾出现 `No space left on device`。仅清理了安全范围内的临时和轮转日志：

- `/tmp/multiace-preflight/*`
- `/home/lava/printer_data/logs/` 下的轮转旧日志

未删除配置、当前日志、G-code 或备份。清理后根分区从约 `79%` 降到约 `47%`，
日志目录从约 `343M` 降到约 `31M`。
