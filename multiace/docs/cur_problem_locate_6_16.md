## 2026-06-16 实机多色换料异常 — 问题定位与修复建议

### 事件概述

实机多头多色打印测试中出现阻塞级安全问题：一次换料异常中，**旧料未真正退出，但系统继续触发目标 ACE slot 进料**。用户观察到换料静止阶段工具头明显抖动，状态不健康后直接断电终止测试。

日志证据：
- route 目标明确（`HEAD=<n> ACE=0 SLOT=<n>`），无 source graph 映射错误迹象
- 多次 unload 后立即 load，load 阶段出现 `phase3 weak-success` + `wheel delta=0/0` 弱成功记录
- 表明执行状态机可能把不充分的 unload/load 证据提交为成功

---

### 一、根因定位

#### 根因 1：unload 无条件清除 `head_source`（CRITICAL）

**文件：** `multiace/klipper/extras/filament_feed_ace.py`

两处 unload 路径均有相同缺陷。

**路径 A — `FEED_UNLOAD_STAGE_DOING`（line 2075–2118）：**

```python
# line 2075
unload_max = self.ace.unload_retry if self.ace is not None else 3
unload_ok = False
for unload_attempt in range(unload_max):
    self.ace.retract_fil(ace_slot)
    self.ace.wait_ace_ready()
    # ... forward probe ...
    if not self.runout_sensor[ch].get_status(0)['filament_detected']:
        unload_ok = True
        break
    # ... retry with re-heat + INNER_FILAMENT_UNLOAD ...

# line 2105 — 仅记录日志，不阻断
if not unload_ok:
    logging.info("[feed][unload] filament genuinely stuck after %d unload attempts (sensor never cleared)", unload_max)

self.ace._last_unload_ok = unload_ok                    # line 2108
self.gcode.run_script_from_command("M104 S0\r\n")       # 关加热
self.channel_error[ch] = FEED_OK                         # line 2110 — 无条件标记 OK
self._set_channel_state(ch, FEED_STA_UNLOAD_FINISH, True)  # line 2111 — 无条件标记完成

# line 2113–2118 — 无条件清除 head_source
if ace_routed and getattr(self.ace, '_ace_mode', '') == 'multi':
    head_idx = self.filament_ch[ch]
    if self.ace._head_source.get(head_idx) is not None:
        self.ace._head_source[head_idx] = None           # ← 即使 unload_ok=False 也清除
        self.ace._save_head_source()
```

**路径 B — 另一 unload 处理分支（line 2205–2247）：** 完全相同的模式，不赘述。

**问题本质：** unload retry 全部失败后（sensor 从未清除），代码仅打一行日志警告，然后：
1. 标记 `FEED_STA_UNLOAD_FINISH`（完成）
2. 标记 `channel_error = FEED_OK`（无错误）
3. 清除 `head_source[head] = None`（声明路径已空）

更隐蔽的情况：如果 sensor 在 unload retry 期间**短暂清除但耗材仍在 PTFE/四通路径中**，则 `unload_ok = True` 同样导致路径未清空却标记成功。

#### 根因 2：unload 成功判定仅依赖 toolhead sensor — sensor cleared ≠ path clear（HIGH）

**文件：** `multiace/klipper/extras/filament_feed_ace.py` line 2089, 2219

```python
# 当前唯一判定标准
if not self.runout_sensor[ch].get_status(0)['filament_detected']:
    unload_ok = True
    break
```

`toolhead sensor cleared` 只能说明工具头挤出机附近的 runout sensor 检测不到耗材，**不能证明耗材已完全退出四通、共享 PTFE 或 source 侧安全位置**。耗材尖端可能恰好缩到 sensor 盲区（距 sensor 几毫米），但耗材体仍在 PTFE 管路中。下一 slot 进料时，新旧料在共享路径中冲突。

当前没有验证：
- source 侧回抽到安全位置（如四通 junction 之外）
- transport 已完全停止
- feed assist 已 disarm

#### 根因 3：ACE_UNLOAD_HEAD 不使用 `_last_unload_ok`（HIGH）

**文件：** `multiace/klipper/extras/ace.py` line 5088–5201

```python
def cmd_ACE_UNLOAD_HEAD(self, gcmd):
    self._last_unload_ok = True                    # line 5095 — 初始化为 True
    # ... 参数校验 ...
    # FEED_AUTO UNLOAD=1 执行（通过 filament_feed_ace.py）
    # filament_feed_ace.py 内部更新 self._last_unload_ok

    self._head_source[head] = None                 # line 5192 — 无条件清除
    self._save_head_source()

    # line 5197 — 仅诊断性日志，不改变行为
    if sensor and sensor.get_status(0)['filament_detected']:
        self.log_error('filament still detected')
    else:
        self.log_always('unload_head_success')
```

与 `ACE_SWAP_HEAD` 对比（line 5737）：swap **确实检查** `_last_unload_ok` 并在失败时进入 `_pause_for_recovery`。但 standalone `ACE_UNLOAD_HEAD` **没有这个检查**。而且即使是 swap 路径，也仅依赖来自 filament_feed_ace.py 的 sensor-only 判定（根因 2）。

#### 根因 4：load 阶段 wheel delta 0/0 弱成功通道（HIGH）

**文件：** `multiace/klipper/extras/filament_feed_ace.py` line 1586–1588, 1601

```python
# line 1586 — retry=0 时恒为 True
step_ok = (retry_extrude == 0
           or step_delta_a >= 2 or step_delta_b >= 2
           or coil_high)

# line 1601 — 只要 coil 通过即可提交成功
if coil_ok or (wheel_ok and step_ok):
    extruded = True
    break
```

首次尝试（retry=0）时 `retry_extrude == 0` 为真 → `step_ok` 恒为 `True`。此时若 coil 信号勉强通过阈值（`coil_ok == True`），即使 `wheel_delta_a = 0` 且 `wheel_delta_b = 0`，也会判定 load 成功并 break。日志中出现的 `phase3 weak-success` + `wheel delta=0/0` 即对应此路径。

sensor-only 路径（line 1635–1665）有防御性日志但不会提交成功——必须最终等 wheel/coil 确认——然而 retry=0 + coil_ok 的组合绕过了这个防御。

#### 根因 5：load 成功后无条件写入 `head_source`（MEDIUM）

**文件：** `multiace/klipper/extras/ace.py` line 5016–5056

```python
try:
    self.gcode.run_script_from_command(
        "FEED_AUTO MODULE=%s CHANNEL=%d EXTRUDER=%d LOAD=1" ...)
except Exception as e:
    # 异常时才标记 load_failed + transport stop
    failed_source['load_failed'] = True
    self._head_source[head] = failed_source
    self._stop_slot_transport(ace_index, slot, 'load_head_failed')
    raise

# 无异常 → 直接设置 head_source，无 post-check
self._head_source[head] = self._head_source_for_slot(ace_index, slot, slot_info)
self._save_head_source()
```

`FEED_AUTO LOAD=1` 不抛异常即视为成功，不考虑 phase3 的弱成功/强成功区别。此时 head_source 直接从"目标 source"覆盖为新的 loaded 状态，**没有 post-check 确认耗材确实到达工具头**。

---

### 二、完整失败链

```
换料触发 (ACE_SWAP_HEAD HEAD=n ACE=0 SLOT=new)
  │
  ├─ 1. unload 当前 source
  │     │
  │     ├─ filament_feed_ace.py: FEED_AUTO UNLOAD=1
  │     │     ├─ retract_fil() + forward probe + runout sensor check
  │     │     ├─ sensor 短暂清除 → unload_ok = True  ← 根因 2
  │     │     │   （耗材缩到 sensor 盲区，但仍在 PTFE 管路中）
  │     │     └─ 无条件: channel_error=FEED_OK, state=UNLOAD_FINISH,
  │     │                head_source=None                 ← 根因 1
  │     │
  │     └─ ace.py ACE_SWAP_HEAD: _last_unload_ok=True → 继续
  │
  ├─ 2. load 新 source
  │     │
  │     ├─ filament_feed_ace.py: FEED_AUTO LOAD=1
  │     │     ├─ phase3 retry=0: step_ok 恒真 (retry_extrude==0)
  │     │     ├─ coil 勉强通过 → extruded=True  ← 根因 4
  │     │     │   wheel delta=0/0, 实际未进料到工具头
  │     │     └─ 不抛异常 → 返回
  │     │
  │     └─ ace.py ACE_LOAD_HEAD: 无异常 → 设置 head_source=新source  ← 根因 5
  │
  └─ 3. 物理层面：旧料堵在 PTFE/四通中 + 新料推进 → 冲突 → 工具头抖动
```

---

### 三、修复建议

#### 修复 1：unload 失败时阻断（CRITICAL）

**文件：** `multiace/klipper/extras/filament_feed_ace.py` 两处 unload 路径

```diff
 if not unload_ok:
     logging.info("[feed][unload] filament genuinely stuck after %d unload attempts (sensor never cleared)", unload_max)
+    # 不清除 head_source，不标记完成，向上抛异常
+    self.channel_error[ch] = FEED_ERR
+    self._set_channel_state(ch, FEED_STA_UNLOAD_FAIL, True)
+    raise self.gcode.error(
+        "Unload failed: sensor never cleared after %d attempts. "
+        "Path may still contain filament. "
+        "Do not load another source without manual clearance."
+        % unload_max)
```

在 unload retry 循环全部失败（sensor 从未清除）时，**不**标记 `FEED_OK`/`UNLOAD_FINISH`，**不**清除 `head_source`，而是设置错误状态并抛出异常。上层 `ACE_UNLOAD_HEAD` / `ACE_SWAP_HEAD` 的 except 块捕获后执行 transport stop 并 pause。

#### 修复 2：ACE_UNLOAD_HEAD 增加 `_last_unload_ok` 检查（HIGH）

**文件：** `multiace/klipper/extras/ace.py` line 5179（在 FEED_AUTO 调用后、head_source 清除前）

```diff
+if not self._last_unload_ok:
+    self._audit_state('UNLOAD_HEAD_FAILED', {
+        'head': head, 'reason': 'last_unload_ok=False'})
+    raise gcmd.error(
+        '[multiACE] ACE_UNLOAD_HEAD HEAD=%d: unload incomplete - '
+        'filament may still be in path. Do not load another source '
+        'until path is confirmed clear.' % head)
+
 self._head_source[head] = None
 self._save_head_source()
```

#### 修复 3：强化 load 成功判定（HIGH）

**文件：** `multiace/klipper/extras/filament_feed_ace.py`

方案 A（最小改动）— retry=0 不允许弱成功：

```diff
-step_ok = (retry_extrude == 0
-           or step_delta_a >= 2 or step_delta_b >= 2
-           or coil_high)
+# retry=0 时 retry_extrude==0 无意义（尚未重试），必须验证 wheel/step/coil
+step_ok = (retry > 0 and retry_extrude == 0) or \
+          step_delta_a >= 2 or step_delta_b >= 2 or coil_high
```

方案 B（推荐）— ACE 路径必须同时满足 wheel 和 step：

```diff
 if ace_routed:
     wheel_ok = (self.check_wheel_data != 0 and
                 (wheel_cnt_a_2 - wheel_cnt_a_1 >= 5 or
                  wheel_cnt_b_2 - wheel_cnt_b_1 >= 5))
     coil_ok = (...)
-    if coil_ok or (wheel_ok and step_ok):
+    # wheel 必须确认有实质运动，不能仅靠 coil
+    if wheel_ok and step_ok:
         extruded = True
         break
```

#### 修复 4：load 成功后增加 toolhead sensor post-check（MEDIUM）

**文件：** `multiace/klipper/extras/ace.py` ACE_LOAD_HEAD line ~5053

```diff
+self.toolhead.wait_moves()
+sensor = self.printer.lookup_object(
+    'filament_motion_sensor e%d_filament' % head, None)
+if sensor and not sensor.get_status(0)['filament_detected']:
+    self._audit_state('LOAD_HEAD_POSTCHECK_FAILED', {
+        'head': head, 'reason': 'sensor_clear_after_load'})
+    self._head_source[head] = {
+        'ace_index': ace_index, 'slot': slot,
+        'load_failed': True, ...}
+    self._stop_slot_transport(ace_index, slot, 'load_postcheck_failed')
+    raise gcmd.error(
+        '[multiACE] LOAD_HEAD HEAD=%d post-check failed: '
+        'sensor reports no filament after FEED_AUTO LOAD=1 completed. '
+        'Filament may not have reached toolhead.' % head)
+
 self._head_source[head] = self._head_source_for_slot(ace_index, slot, slot_info)
 self._save_head_source()
```

在 FEED_AUTO LOAD=1 成功返回后，调用 `wait_moves()` 再采样一次 toolhead sensor。若此时 sensor 仍然无料，说明 load 实际上失败（phase3 误判），拒绝写入 head_source。

#### 修复 5：transport 清理闭环补全（MEDIUM）

**文件：** `multiace/klipper/extras/ace.py`

需要确保下表中所有 unload/load 失败边界都调用 `_stop_slot_transport()` / `_disable_feed_assist_all()`：

| 边界状态 | 当前行为 | 修复 |
|----------|----------|------|
| unload 失败（_last_unload_ok=False） | 缺少 transport stop | 在 ACE_UNLOAD_HEAD 失败路径添加 `_stop_slot_transport` |
| load 失败（FEED_AUTO 异常） | 已有 `_stop_slot_transport` (line 5033) | OK |
| swap load 失败 | 已有 (line 5804, 5819) | OK |
| swap unload 失败 | 进入 `_pause_for_recovery` 但缺少 transport stop | 添加 `_stop_slot_transport` |
| print end | 有 `_disable_feed_assist_all` (line 4811) | OK |
| manual abort | 有 `ACE_STOP_TRANSPORT` | OK |

#### 修复 6：三段判定框架落地（MEDIUM — 架构方向）

当前判定仅依赖 toolhead sensor。文档 `unified_slot_toolhead_flow.md:521` 定义了更完整的判定框架：

1. **toolhead sensor clear** — 工具头 sensor 附近无料（当前唯一判定）
2. **source/path clear** — 耗材已退到 source 安全位置（如四通 junction 外）
3. **transport/feed assist stopped** — 主动驱动已停止

第 2 层需要 ACE/native adapter 提供 source 侧回抽位置反馈。第 3 层在 ace.py 中已有 `_stop_slot_transport()` / `_disable_feed_assist_all()`，需要在 unload 完成路径中强制调用并验证返回状态。

短期方案：unload 完成后在判断 `_last_unload_ok` 前，增加以下 post-checks：
- 调用 `_stop_slot_transport(ace_index, slot, 'unload_complete')` 并检查无异常
- 调用 `_disable_feed_assist_all()` 并验证 `_feed_assist_index == -1`

---

### 四、验证要求

修复后需验证以下场景（dry-run 优先，实机短距离逐步）：

1. **unload 失败不污染状态**
   - mock unload retry 耗尽、sensor 从未清除
   - 预期：`head_source` 保留原值，channel state 为 `UNLOAD_FAIL`，后续 swap/load 被拒绝

2. **wheel delta 0/0 不提交 loaded**
   - mock check_wheel_data=1 但 wheel counts 无变化
   - 预期：`extruded` 保持 `False`，进入 retry 或最终失败

3. **load post-check 拦截假成功**
   - mock FEED_AUTO LOAD=1 完成但 post-check sensor 无料
   - 预期：拒绝写入 `head_source`，抛异常

4. **旧料未退出 → 新料被阻止**
   - mock unload 路径不完整 + 后续 swap/load 请求
   - 预期：swap/load 在 pre-check 或 unload 阶段被拒绝，进入 pause/recovery

5. **transport 在失败边界全部停止**
   - 覆盖 unload 失败、load 失败、print end、manual abort
   - 预期：`_feed_assist_per_ace` 全部为 -1，`_feed_assist_index` = -1

---

### 五、相关文档索引

| 文档 | 内容 |
|------|------|
| `source_graph_architecture.md:1367` | 事件复盘、已确认风险、阻塞 TODO |
| `unified_slot_toolhead_flow.md:521` | 安全不变量：sensor cleared ≠ path clear |
| `post_mvp_optimization_plan.md:319` | 安全阻塞：安全闭环完成前暂停效率优化 |

### 六、影响代码文件清单

| 文件 | 行号 | 问题 | 严重度 |
|------|------|------|--------|
| `filament_feed_ace.py` | 2105–2118 | unload 失败无条件清除 head_source + 标记完成 | **CRITICAL** |
| `filament_feed_ace.py` | 2235–2247 | 同上（第二 unload 路径） | **CRITICAL** |
| `filament_feed_ace.py` | 2089–2092 | unload 判定仅依赖 toolhead sensor | **HIGH** |
| `filament_feed_ace.py` | 1586–1588 | retry=0 时 step_ok 恒真（retry_extrude==0） | **HIGH** |
| `filament_feed_ace.py` | 1601 | `coil_ok or (wheel_ok and step_ok)` — coil 可绕过 wheel 检查 | **HIGH** |
| `ace.py` | 5095 | `_last_unload_ok` 初始化为 True | MEDIUM |
| `ace.py` | 5192–5193 | ACE_UNLOAD_HEAD 无条件清除 head_source，不检查 `_last_unload_ok` | **HIGH** |
| `ace.py` | 5054–5056 | load 成功后无条件写入 head_source，无 toolhead sensor post-check | MEDIUM |
| `ace.py` | 5737–5758 | ACE_SWAP_HEAD 检查 `_last_unload_ok` 但依赖 sensor-only 判定，且 pause 路径缺少 transport stop | MEDIUM |

---

生成日期：2026-06-16
基于分支：source-graph-architecture