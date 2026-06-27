# Colorful-U1 Frontend UI Rewrite Plan

日期：2026-06-09

本文记录 Colorful-U1 前端 UI 重构方案。此次重构不是在旧 multiACE UI 上继续
修改，而是基于当前后端 `source graph / source state / route plan` 架构重写
前端。旧 UI 只作为交互思路参考，不复用旧状态模型。

进退料执行语义见：
`multiace/docs/unified_slot_toolhead_flow.md`。前端所有硬件操作入口必须遵守
该文档里的 source slot / toolhead 职责拆分。

## 当前实现状态：2026-06-09

前端已经开始重写，并进入 dry-run 可验证阶段，但还没有达到最终 UI 质量。

已落地：

- 品牌切换为 `Colorful-U1`。
- 三个主 Tab：控制台、配置、上传打印。
- 控制台接入 `/api/state`、`/api/source-graph`、`/api/source-state`。
- Toolhead 和 Source 卡片已基于 source graph/source state 展示。
- Source 卡片显示 runtime 状态：
  - native source 显示 `detect / extruder / channel`；
  - ACE slot 显示 slot state；
  - 不可用 source 显示原因。
- 上传打印页走 route-plan flow：
  - upload/preview；
  - manual remap；
  - auto validate；
  - route-plan/print；
  - print job status。
- 上传映射页会显示 configured sources；不可用 source 可见但禁用，避免用户误以为
  source 消失。
- 配置页 `Source 执行` 已增加 per-source 预进料长度入口：
  - Native source preload：逐 `native:<n>` 配置；
  - ACE source preload：逐 `ace:<ace>:<slot>` 配置；
  - 保存到 `source_graph.json` 的 `source.execution.preload_length_mm`；
  - 保存后重新读取 source graph/source state，并让 Klipper 刷新 source graph
    缓存。
- 上传映射页已经区分三段状态：
  - 上传后 resolver 未匹配全部 tool：`需要手动映射 T...`；
  - 用户已选齐 source 但尚未应用：`待应用`；
  - `/api/route-plan/remap` 和 route validate 通过：`ok`。
- 手动选择 source 只更新本地映射卡片，不自动调用 `/api/route-plan/remap`。
  只有用户点击“应用映射”时才一次性提交完整 `tool_targets`。
- dry-run 已使用真实 Snapmaker Orca U1 G-code 验证 manual remap 后可发送。
- 真实 G-code 浏览器 dry-run 验证结果：
  - 选齐 T0-T3 source 前后 `/api/route-plan/remap` 请求数保持 0；
  - 点击“应用映射”后只产生 1 次 remap 请求；
  - Mapping/Route validate/Source state 最终为 `ok/ok/clean`；
  - `发送打印`按钮只在 route validate 通过后解锁；
  - console 无 warning/error。

仍需收口：

- 当前 UI 仍偏工程调试态，视觉和交互层级需要继续打磨。
- 控制台需要重新拆分 source slot 与 toolhead 操作语义：
  - Source 卡片只做耗材信息、source 状态、执行参数、完全退料。
  - Toolhead 卡片才负责 Load / Unload / Swap 主流程。
  - Unload 不允许选择 source，只能退当前 head 的 `current_source`。
  - `native:<n>` 在 UI 中应显示为 0-based `Native Slot <n>`，不能显示成容易和
    工具头混淆的 `native Tn`。
  - `head:<n>`、`native:<n>`、`ace:<ace>:<slot>` 在 UI、日志、API、配置中都
    使用 0-based 编号，不再提供 `display_index_base` 之类的显示偏移开关。
- 上传页还需要继续增强 G-code 被安全校验拒绝、route plan stale、source graph
  hash 变化等状态的视觉提示。
- 自动 resolver 无法匹配全部 slicer tools 时，不能让 `route_plan=null` 表现得像
  隐式错误；应直接提示用户完成映射。
- 控制台硬件动作的队列/弹窗交互需要移除或隐式化。常用 load/unload/swap 应保持
  直接：用户选定 source/head 后立即调用后端单 operation，后端用 active operation
  lock 防止并发；前端不再维护可见队列。
- 配置页已经开始区分设备通用配置、source 专属执行参数和材料信息；后续仍需
  继续整理 load/unload/retract/purge 等更多 source 专属参数。
- 模型预览仍是 MVP，需要后续增强为更可信的 G-code 检查/预览入口。

## 重构目标

- 去除原 UI 中的 multiACE / mUlt1ACE 品牌露出，前端显示统一为
  `Colorful-U1`。
- 前端语义直接建立在 source graph、source state、route plan 上。
- 不再暴露 `native / ace mode` 二选一模型。
- 支持任意 source 映射到任意 head 的新架构。
- 支持后续多 native、多 ACE、native + ACE 同 head 混合换料、提前换料算法。
- 将控制、配置、上传打印三个主要工作流清晰分离。
- 提供现代、简洁、稳定的 Apple 风格设备控制界面。

## 非目标

- 不继续修补旧 UI。
- 不在前端重构第一阶段实现复杂换料优化算法。
- 不在第一阶段做完整工业级 3D G-code 渲染器。
- 不绕过后端 route plan 校验直接发送打印。
- 不恢复旧 `mode=optimize/layer` 打印路径。

## 技术边界

当前前端技术栈：

- `multiace/web/frontend/index.html`
- `multiace/web/frontend/app.js`
- `multiace/web/frontend/style.css`
- Vue runtime 直接在页面中使用，无构建链。

重构建议：

- 保持无构建链方案，降低打印机部署复杂度。
- `index.html`、`app.js`、`style.css` 全量重写。
- 可以按需增加少量静态 JS 文件，但不引入大型 bundler。
- 后续如确实需要 Three.js/G-code preview 库，再作为单独阶段评估。

## 视觉方向

整体参考 Apple 风格：

- 浅色优先，深色可后续作为主题。
- 大面积留白，低对比边框，柔和阴影。
- 卡片圆角控制在 8px 以内。
- 文案简短直接。
- 信息层级明确，避免旧 UI 的密集配置列表感。
- 控制按钮用清晰的图标和短标签。
- 危险动作使用明确确认和命令预览。

注意：

- 这是设备控制台，不是营销 landing page。
- 首屏必须直接呈现控制台状态，不做 hero。
- 所有状态、按钮、错误都要适合实机调试场景。

## 信息架构

主导航固定为三个 Tab：

1. 控制台
2. 配置
3. 上传打印

顶部栏包含：

- `Colorful-U1` 品牌名。
- 三段式 Tab 导航。
- 连接状态。
- 打印机状态。
- 语言切换。
- 版本号和打印机信息。

## 控制台 Tab

控制台是日常使用主界面，面向运行时管理。

### 状态概览

显示：

- printer state。
- active toolhead。
- active ACE。
- source graph hash 状态。
- 当前 route/source state 是否异常。
- 通知和错误摘要。

异常状态必须醒目：

- `unknown`
- `stale`
- `failed`
- `exhausted`

其中 `exhausted` 表示 ACE slot 已 empty，但路径中可能仍有余料，不能按普通
empty/load 处理。

### Toolhead 卡片

每个 T0-T3 一张卡片。

显示：

- 工具头在线状态。
- 工具头传感器是否有料。
- `current_source`。
- `source_confidence`。
- 当前耗材材料、颜色、品牌、预设名。
- 可用 source 数量。
- 当前 channel state / error。

动作：

- Load。
- Unload。
- Swap / Select source。
- Recover。
- Clear stale/failed 状态。

要求：

- 不显示 `native / ace mode` 下拉框。
- 点击卡片打开详情面板。
- Load：用户选择目标 source 后执行；source 必须来自该 head 可达 source 列表。
- Unload：不允许选择 source，后端使用该 head 的 `current_source`。
- Swap：用户选择目标 source，后端负责退当前 source 并装载目标 source。
- 动作执行前可显示 route/source action preview，但执行必须是一个后端 operation，
  不是前端排队的多个小动作。
- `unknown/stale/failed/exhausted` 状态阻止自动动作，并显示恢复指引。

### Source 卡片

统一管理所有耗材来源：

- `native:0..3`
- `ace:<ace>:<slot>`
- 后续扩展的其它 source 类型

每张 source 卡片显示：

- source id。
- 类型：Native Slot / ACE Slot。
- 可达 heads。
- 耗材信息。
- presence。
- slot_state。
- path_position。
- ready / empty / error / exhausted。
- execution profile。
- 回抽配置摘要。

筛选：

- All。
- Native。
- ACE。
- Loaded。
- Empty。
- Error。

动作：

- 编辑耗材信息。
- 编辑 source 执行参数。
- 完全退料。
- source recovery。

禁止：

- 不在 Source 卡片上执行“进某个工具头”的完整 load。
- 不在 Source 卡片上表达 loaded toolhead；工具头 loaded 状态只显示在
  Toolhead 卡片中。

### ACE 管理

每台 ACE 一张卡片。

显示：

- ACE 编号。
- 当前协议和连接状态。
- 温度。
- 湿度。
- dryer 状态。
- slot 1-4 状态。

动作：

- 切换 active ACE。
- 启动烘干。
- 停止烘干。
- 打开 slot/source 详情。

slot 不再表达“归属哪个头”，而表达：

`ace:N:S` 是一个 source，并通过 source graph edge 连接到可用 head。

### 耗材库和预设

保留并升级原有 override/snapshot 思路。

功能：

- 材料预设。
- native source material override。
- ACE slot material override。
- 保存当前 loadout 为 preset。
- 从 preset 恢复耗材信息。
- 记录材料、颜色、品牌、子类型、备注。

第一阶段只做基础管理，不实现复杂数据库。

### 显示屏和摄像头

显示屏：

- 保留原有 screen 映射功能。
- 支持内嵌显示和弹出。
- 不可用时显示明确状态。

摄像头：

- 新增 camera 区域。
- 初期支持配置 MJPEG / iframe / image URL。
- 不和打印流程强绑定。
- 后续再扩展多摄像头、截图、延迟检测。

## 配置 Tab

配置页必须区分通用配置和 source 专属配置。

### 打印机通用配置

用于经常变更、但不属于某个 source 的配置。

示例：

- 默认温度。
- 默认换料参数。
- 通知设置。
- UI 语言。
- Web/API 行为设置。
- 安全开关。

### Source Graph 配置

这是新配置页核心。

对象：

- heads。
- sources。
- edges。
- profiles。

交互建议：

- 左侧 Heads。
- 中间 Sources。
- 右侧 Inspector。
- 勾选 source -> head 可达关系。
- 设置 edge enabled。
- 设置 edge priority。
- 设置 edge constraints。
- 保存按钮明确写 `保存 Source Graph`。

保存行为：

- 只保存配置。
- 不重启 Klipper。
- 不移动硬件。
- 保存后提示已有 route plan 失效，需要重新 preview/validate。

### Source 专属配置

按 source 分组显示。

示例：

- `native:0` feeder 配置。
- `ace:0:1` slot 配置。
- preload length。
- push to junction length。
- load to toolhead length。
- unload to junction length。
- full unload length。
- feed/retract speed。
- feed assist 参数。
- 温度策略。
- purge/prime 参数。
- recovery 策略。

要求：

- 不和打印机通用配置混在一个列表中。
- 明确标识该配置影响哪个 source。
- source 配置只影响 source slot 侧动作，不代表工具头已装载。
- 危险配置需要说明影响范围。

### Execution Profiles

用于描述 source action 的命令模板。

对象：

- native load/unload profile。
- ACE load/unload/swap profile。
- 后续 tuned profile。

要求：

- 显示 command preview。
- 对危险字段给提示。
- 保持后端校验为最终安全边界。

### Raw Config Editor

保留为高级功能。

要求：

- 默认折叠。
- 保存前提示会过滤 obsolete keys。
- 不自动重启 Klipper。
- 重启按钮独立。

## 上传打印 Tab

上传打印页替代旧 G-code button。

正式流程：

1. 上传 G-code。
2. G-code 检查。
3. 模型/路径预览。
4. 耗材映射和 route plan。
5. 校验并发送打印。

### 上传结果

显示：

- 文件名。
- 文件大小。
- slicer tools。
- 解析到的材料和颜色。
- source graph hash。
- route plan hash/摘要。
- resolve errors。
- warnings。

### G-code 检查

显示：

- 不支持命令。
- Bambu/P1S 风格文件拦截。
- 缺失 tool mapping。
- route plan 缺 event/target。
- source state stale。
- source confidence 异常。

错误必须阻止发送打印。

### 模型预览

第一阶段目标：

- 简化 G-code preview。
- 显示 bounding box。
- 显示层范围。
- 显示工具切换点。
- 显示颜色段。

实现建议：

- 先用 Canvas 做轻量 2D/层预览。
- 暂不做复杂 3D 渲染。
- 后续独立评估 Three.js 或专用 G-code preview 库。

### 耗材映射

布局：

- 左侧 slicer tools。
- 右侧可用 sources。
- 中间显示当前自动匹配。

每条 mapping 显示：

- slicer T。
- source。
- head。
- material。
- color。
- confidence/tier。
- commands preview。

人工修改：

- 使用 `/api/route-plan/remap`。
- 不在打印阶段传 `tool_targets`。
- 选择 source 时只更新前端本地映射，不自动 remap，避免半完成映射触发
  `manual mapping missing T...`。
- 选齐所有 slicer tools 后，Mapping 状态显示 `待应用`。
- 点击“应用映射”后一次性提交完整 `tool_targets`，并自动 route validate。
- route validate 通过后，Mapping 状态显示 `ok`，发送打印按钮才解锁。
- 若用户再次修改映射，已有 validate 结果必须失效并重新进入待应用/待校验状态。

当前状态：

- 2026-06-08 dry-run 已确认真实 U1 G-code 可以手动 remap 后发送。
- 2026-06-09 已修复选择 source 时自动 remap 的中间态 400 问题。
- 2026-06-09 已修复“选齐但未应用”时仍显示旧 resolver 错误和误报 Mapping
  `ok` 的问题。

### 推荐算法入口

第一阶段只展示已有统计和建议入口。

显示：

- active ACE swaps。
- skipped same source。
- estimated swap time。
- events sample。

按钮预留：

- 推荐更少换料。
- 推荐相近颜色。
- 推荐按空闲头预加载。

第一阶段建议只做 suggestion preview，不自动应用。

### 发送打印

发送前必须：

- 调用 `/api/preflight/route-plan/validate`。
- 校验通过才允许 `/api/route-plan/print`。

阻止发送的情况：

- route plan missing。
- source graph hash changed。
- route plan stale。
- source state changed。
- `unknown/stale/failed/exhausted`。
- profile command tampered。
- resource conflict。

## API 使用边界

新 UI 应使用：

- `GET /api/state`
- `GET /api/source-graph`
- `POST /api/source-graph`
- `GET /api/source-state`
- `POST /api/source-action/preview`
- `POST /api/source-actions/preview`
- `POST /api/source-transition/preview`
- `POST /api/route-plan/preview`
- `POST /api/route-plan/remap`
- `GET /api/preflight/route-plan/validate`
- `POST /api/route-plan/print`
- `GET /api/preflight/print/status`

新 UI 禁止依赖：

- `state.route.head_modes`
- `state.route.ace_targets`
- `state.route.primary_head`
- `/api/upload-and-print`
- `mode=optimize`
- `mode=layer`
- 打印阶段 `tool_targets` override

## 安全交互要求

- 配置保存不自动执行硬件动作。
- 硬件动作前显示命令预览或阶段摘要。
- 同一时刻只展示一个 active operation；新动作在 busy 时禁用。
- 前端不再提供可见操作队列的 clear/隐藏语义，避免用户误以为能取消已经发出的
  硬件动作。
- Source 卡片的完全退料必须在 source 未被任何 toolhead 装载时才可点击。
- Toolhead unload 不允许用户选择 source。
- 打印发送前强制 route plan validate。
- source graph 保存后提示旧 route plan 失效。
- `unknown/stale/failed/exhausted` 必须阻止自动动作。
- 恢复动作要明确写出用户需要做什么。
- 所有真实动作都通过后端显式参数校验。

## 实施阶段

### Phase 1：基础壳和状态模型

- 重写 `index.html`。
- 重写 `app.js`。
- 重写 `style.css`。
- 切换品牌为 `Colorful-U1`。
- 建立 API client。
- 建立统一 state store。
- 接入 `/api/state`、`/api/source-graph`、`/api/source-state`。
- 完成三 Tab 基础布局。
- 完成连接状态和错误状态。

### Phase 2：控制台

- Toolhead cards。
- Source cards。
- ACE cards。
- Material preset 基础管理。
- Load/unload/swap preview。
- Dryer 控制。
- Screen 映射。
- Camera placeholder 和基础配置。

### Phase 3：配置页

- Source graph editor。
- Edge editor。
- Source 专属配置面板。
- Execution profile 面板。
- 通用配置面板。
- Raw config editor 高级入口。

### Phase 4：上传打印页

- G-code upload。
- Preflight result。
- G-code check。
- 模型/路径 preview MVP。
- Route plan display。
- Manual remap。
- Validate。
- Print。
- Print job status。

### Phase 5：视觉和交互收口

- Apple 风格视觉统一。
- 响应式布局。
- 空状态。
- loading 状态。
- 错误状态。
- 多语言文案整理。
- 去除残留 multiACE 品牌文案。

## 验收标准

基础验收：

- 页面品牌显示为 `Colorful-U1`。
- 三个 Tab 正常工作。
- 不再显示 native/ace mode 配置下拉。
- Dashboard 使用 source graph/source state 展示工具头和 source。
- 上传打印必须通过 route plan validate。
- `/api/upload-and-print` 不被前端调用。
- 旧 `state.route.head_modes/ace_targets` 不作为配置来源。

安全验收：

- route plan missing 时无法发送打印。
- source graph hash 改变后旧计划无法发送。
- source state stale 后旧计划无法发送。
- `unknown/stale/failed/exhausted` 有明确阻止和恢复提示。
- 硬件动作执行前能看到命令预览。

体验验收：

- 1366px 桌面宽度下无明显拥挤。
- 390px 移动宽度下无文字重叠。
- 卡片内文字不溢出。
- 主要动作有 loading/disabled/error 状态。
- 控制台首屏能看出整机状态、工具头状态和 source 异常。

## 后续移除旧字段条件

完成前端重构并验证后，才继续后端清理：

1. Dashboard 不再读取 `/api/state.route`。
2. 前端完全使用 source graph 配置 topology。
3. 打印页完全使用 route plan flow。
4. Klipper 无 source graph 时改为明确错误，不 fallback 到旧 `head_modes`。
5. post-processor CLI 旧 fallback 隔离到离线工具模式。
