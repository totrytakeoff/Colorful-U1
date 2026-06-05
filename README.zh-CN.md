# Colorful-U1

Colorful-U1 是一个面向 Snapmaker U1 的实验性耗材路由、固件扩展与 Web 控制项目。它基于 [decay71/multiACE](https://github.com/decay71/multiACE)，而 multiACE 又基于 [BlackFrogKok/SnapAce](https://github.com/BlackFrogKok/SnapAce)。

这个分支的核心目标是让 U1 的原生进料头和 Anycubic ACE 设备可以通过明确、可测试的方式混合工作。当前已验证的 MVP 支持在同一份 G-code 中同时使用 native 工具头和 ACE 工具头，并在 Web 发送打印前把切片里的工具编号映射到真实的 native 耗材或 ACE 槽位。

> 警告：这是会直接控制硬件的 beta 软件。错误的工具头映射、错误的退料长度、残留状态或接线不一致，都可能导致打印失败，甚至对硬件造成压力。请先用小模型测试，确保可以随时停止打印或断电，不要把它当成稳定量产固件使用。

## 项目来源

- **SnapAce**：最早的 Snapmaker U1 + Anycubic ACE Pro 接入方案。
- **multiACE**：在 SnapAce 基础上增加多 ACE、Web UI、Web Preflight、更稳健的进退料和打印中换料流程。
- **Colorful-U1**：在 multiACE 基础上继续演进，重点转向 U1 的灵活混合路由：
  - 一个物理工具头完全接管一个 ACE，实现单头 4 色打印；
  - native 工具头和 ACE 工具头可以在同一份 G-code 中协同；
  - native 工具头和 ACE 槽位都支持持久化耗材/颜色配置；
  - 使用显式的工具头、ACE、槽位命令，避免依赖隐式猜测。

本仓库保留 GPL-3.0 许可证和上游历史。

## 当前状态

已经在以下 Snapmaker U1 拓扑上验证：

```text
T0: native
T1: native
T2: native
T3: ACE head
ACE0 -> T3
```

Web Preflight 已测试可以把切片工具映射成类似下面的真实目标：

```text
Slicer T0 -> native T0
Slicer T1 -> ACE0 Slot1 -> physical T3
Slicer T2 -> native T1
Slicer T3 -> native T2
```

生成后的打印文件中，native 目标继续使用普通 `T<head>` 工具切换；ACE 目标会使用显式 ACE 命令：

```gcode
T3
ACE_SWAP_HEAD HEAD=3 ACE=0 SLOT=1
```

## 主要功能

- Dashboard 工具头拓扑配置：
  - 每个 U1 工具头可以配置为 `native` 或 `ace`；
  - 每台 ACE 可以分配给指定的 ACE 工具头；
  - 配置修改会先暂存，再由用户明确提交，不会每改一个下拉框就自动重启 Klipper。

- Web 发送打印前预检查：
  - 读取切片工具、耗材颜色和耗材材料；
  - 读取当前打印机实际装载状态；
  - 把每个切片工具解析到 native 工具头或 ACE 槽位；
  - 自动匹配不理想时支持手动覆盖映射；
  - 在发送打印前拦截重复目标或不可用目标。

- 耗材信息持久化：
  - ACE 槽位信息通过 `slot_overrides.json` 保存；
  - native 工具头信息通过 `native_overrides.json` 保存；
  - Preflight 优先使用持久化配置，而不是只依赖容易重启丢失的 `print_task_config` 运行态数据。

- 安全边界：
  - `ACE_LOAD_HEAD` 和 `ACE_SWAP_HEAD` 必须显式指定 `HEAD`、`ACE`、`SLOT`；
  - native 工具头拒绝走 ACE load/swap/unload 路径；
  - ghost-head 检查只作用于 ACE 模式工具头；
  - Web 后端会校验直接 `FEED_AUTO` 的 native 进退料路由。

- Docker dry-run：
  - 使用 mock Moonraker，在无硬件环境下测试 UI 和 Preflight；
  - 支持模拟 native/ACE 混合状态；
  - 可做 API 级别的预检查和手动映射验证。

## 典型使用流程

1. 打开 Colorful-U1 Web UI：

   ```text
   http://<printer-ip>/multiace/
   ```

2. 在 Dashboard 中配置：
   - 哪些工具头是 `native`；
   - 哪个工具头是 `ace`；
   - 哪台 ACE 归属于该 ACE 工具头。

3. 配置耗材信息：
   - native 工具头卡片保存每个 native 头的材料和颜色；
   - ACE 槽位卡片保存每个 ACE 槽位的材料和颜色。

4. 通过 Colorful-U1 Web Preflight 上传切片生成的 G-code。

5. 检查映射表：
   - native 切片工具应该映射到 `Native Tn`；
   - ACE 切片工具应该映射到 `ACE n Slot m -> Tn`；
   - 如果自动匹配不对，使用手动映射覆盖。

6. 只有在映射结果和真实物理接线、耗材装载一致时再发送打印。

## 已知限制

- 当前自动映射算法偏保守，还没有针对最少换料次数做优化。
- ACE 换料可用，但速度仍然很慢。
- 擦料、冲刷和退料策略目前偏保守。
- ACE 槽位目前仍按 ACE 设备整体分配给某个 ACE 工具头；任意槽位映射到任意工具头仍在规划中。
- 单个 native 工具头配合多个普通 native 进料器的多色方案尚未实现。
- 这个分支目前面向硬件实验，不是开箱即用的终端用户产品。

## 路线图

近期工作：

- 优化映射算法，减少不必要换料；
- 在 Preflight 中更清晰地显示最终生成的命令意图；
- 区分可恢复警告和硬阻断错误；
- 增加 native-only、ACE-only、mixed-routing 的 dry-run 回归测试；
- 基于真实失败日志继续调整进退料参数。

长期工作：

- 单 native 工具头 + 多普通进料器；
- ACE slot 到工具头的任意映射；
- 多个 ACE 工具头；
- 多台 ACE 参与混合路由；
- 更完善的 purge/wipe 策略。

当前工程记录和 TODO 见 [native/ACE MVP plan](multiace/docs/native_ace_mvp_plan.md)。
换料效率优化与切片软件集成路线见
[post-MVP optimization plan](multiace/docs/post_mvp_optimization_plan.md)。

## 安装与测试

Colorful-U1 当前沿用 multiACE 的安装模型。原始安装器和部署脚本仍在 `multiace/` 目录中。

开发和 dry-run 测试：

```bash
docker compose -f multiace/docker-dryrun/docker-compose.yml up -d --build
```

然后打开：

```text
http://127.0.0.1:7126/
```

部署到真实打印机前，请先阅读上游 multiACE 安装说明，并检查本分支修改过的文件。不要在机器正在打印时盲目部署。

## 上游致谢

Colorful-U1 基于以下项目和社区工作：

- [BlackFrogKok/SnapAce](https://github.com/BlackFrogKok/SnapAce)
- [decay71/multiACE](https://github.com/decay71/multiACE)
- Snapmaker U1、ACE Pro 和 Klipper 社区测试工作

再分发本分支时，请保留上游署名和许可证信息。
