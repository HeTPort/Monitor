# Monitor 2.1 打包与设备最小闭环测试计划

更新于 2026-09-01。本计划用于 Kirin9030 实机验收，也可通过替换平台/profile 用于其他板卡。

## 1. 本轮要证明什么

最小闭环只证明以下链路：

```text
PC run -> 设备 agent -> workload -> 指定 UART -> PC 协议解析和判错
                         |
                         +-> 设备本地追加证据
```

probe、pair、deploy 和 verify-deployment 是显式准备，不属于每次 `run`。telemetry 是独立能力；baseline 是可选校验能力。Monitor 不负责修改或恢复 governor、频率、CPU online、功耗策略及 affinity。

## 2. 测试前提

- PC 能通过 HDC/ADB 调用设备 shell。
- PC 与设备 UART 已物理连接。
- workload 二进制与 GPU shader 已放入打包资源目录。
- `avs-uart-relay` 已用与 workload 相同的 OpenHarmony ABI/toolchain 构建并放入平台配置的 `relay.local_asset`。
- 测试者知道设备序列号、PC 串口和设备 UART；0831 记录优先使用 `/dev/ttyHW0`、9600 baud。
- 网络和 GitHub 连接不在本计划判定范围内。

PowerShell 变量：

```powershell
$MON = '.\dist\monitor.exe'
$DEVICE = '<HDC设备序列号>'
$PC_SERIAL = 'COM4'
$DEVICE_UART = '/dev/ttyHW0'
```

从源码验证时使用 `$MON = 'python main.py'` 不适合 PowerShell 的 `& $MON` 多词调用；应直接把下面的 `& $MON` 替换成 `python main.py`。

## 3. 构建和离线回归

### PKG-01 单元与协议回归

```powershell
python -m unittest discover -s tests -v
```

通过条件：全部测试通过。

### PKG-02 发布资源校验

```powershell
python main.py validate --package
```

### PKG-03 构建 exe

```powershell
.\scripts\build.ps1
.\dist\monitor.exe --version
```

通过条件：构建脚本退出 0，版本显示 2.1.0，打包校验没有缺少 agent、relay、workload 或 shader。若源码仓库未提供真实板端二进制，PKG-02/03 必须标为“发布资源未就绪”，不能伪记为代码回归失败或设备失败。

## 4. 每个平台/BSP 做一次的准备

以下命令不应被 `run` 隐式重复。

### PRE-01 平台身份和能力探测

```powershell
& $MON --transport hdc --device $DEVICE --json probe --platform kirin9030 --full
```

通过条件：

- 平台身份明确为 Kirin9030；无法确认时 fail-closed。
- CPU policy/core 映射能被规范化读取。
- governor 等能力只被读取，不被写入。
- telemetry 路径中必需/可选缺失被明确区分。
- 多行或含额外文本的探测输出不破坏最终 JSON。

保存此输出，平台、BSP 或 framework 版本变化后才重做。

### PRE-02 串口配对

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART pair --platform kirin9030 --verify
```

通过条件：显示设备 UART 到 PC COM 的成功映射，后续可省略显式串口参数并复用保存结果。

### PRE-02A Relay ABI/运行能力（每个 BSP 做一次）

部署前可先记录 ABI；此时 relay 未部署导致退出 5 是预期准备信息：

```powershell
& $MON --transport hdc --device $DEVICE --json relay probe --platform kirin9030
```

按输出 ABI 用 workload 的 OpenHarmony toolchain 构建并放到 `serial.relay.local_asset`。完成 PRE-03 部署后执行零负载检查：

```powershell
& $MON --transport hdc --device $DEVICE --device-uart $DEVICE_UART --json relay probe --platform kirin9030 --check-uart
```

通过条件：version 为 `avs-uart-relay 1.0.1`，自检、UART open/termios/tcdrain 全部成功；检查不向 UART 发送测试负载。平台配置应包含 `serial.tail_guard_bytes: 64`。本次修复改变了板端 relay 和 agent，旧部署必须重新执行 PRE-03，不能只替换 PC exe。

### PRE-03 CPU/GPU 部署

```powershell
& $MON --transport hdc --device $DEVICE deploy --profile cpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE deploy --profile gpu_stress_kirin9030
```

通过条件：两个命令均退出 0，部署清单 `complete=true` 且 `verified=true`。部署应包含：

- `/data/local/tmp/avs/bin/avs-device-agent`
- `/data/local/tmp/avs/bin/avs-uart-relay`
- `/data/local/tmp/avs/bin/avs-telemetry-agent`
- CPU/GPU workload
- workload 配置、GPU shader
- 两个 profile 的 telemetry plan

### PRE-04 只读部署核验

```powershell
& $MON --transport hdc --device $DEVICE verify-deployment --profile cpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE verify-deployment --profile gpu_stress_kirin9030
```

通过条件：两个命令均退出 0，哈希一致；设备文件没有被重新部署或修改。

## 5. 最小闭环验收

以下 MC-01 至 MC-05 全部通过，即“启动 agent 和负载、定向串口、PC 判错、设备留证”的最小闭环完成。无需 baseline，也无需 telemetry。

### MC-01 CPU run

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile cpu_stress_kirin9030 --test-id MC-CPU-0831 --attempt-id MC-CPU-0831-001 --pc-artifacts full
```

通过条件：

- 进程退出码为 0；
- 输出 `validation_mode=error-only`、`verdict=PASS`；
- UART v2 收到同一 attempt 的合法连续紧凑事件和 `agent_final`；PC 结果记录 `agent_final_seen=true`；
- `serial.raw` 以完整 FINAL 分隔符结束，下一次 run 开头没有上一 attempt 的 FINAL 尾字节；EOF NUL guard 产生的空帧不计入事件数；
- 没有要求 baseline、probe 或 deploy。

### MC-02 GPU run

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile gpu_stress_kirin9030 --test-id MC-GPU-0831 --attempt-id MC-GPU-0831-001 --pc-artifacts full
```

通过条件与 MC-01 相同，workload 为 GPU profile；`DEBUG:/TRACE:/INFO:` 进入设备 `workload-diagnostics.log`，不产生 DUT 错误。

### MC-03 短 profile 仍使用核心 run 链路

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile cpu_smoke_kirin9030 --test-id MC-SMOKE-0831 --attempt-id MC-SMOKE-0831-001
```

通过条件：退出 0、`validation_mode=error-only`、`verdict=PASS`；设备事件中没有 golden 生成行为。它证明短测试也只使用同一条 run 链路。旧 `smoke` 仅作为弃用兼容别名，不纳入新测试命令。

### MC-04 错误能被判出

使用一个明确返回非零或输出合法 `workload_result=FAIL` 的测试 profile，再执行：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile '<故障注入profile>' --test-id MC-NEG-0831 --attempt-id MC-NEG-0831-001 --pc-artifacts full
```

通过条件：不能显示 PASS；DUT 明确失败退出 1、无有效 workload 结论退出 2、协议/agent/串口错误退出 3，并在 `result.json` 中给出简短原因。agent transport 未结束也必须退出 3，且 PC 命令不能继续等待到完整 HDC 超时时间。若当前发布包没有故障注入 profile，可用离线协议测试覆盖，设备项标记待补，不能用拔网线替代。

### MC-05 设备证据可拉取且默认保留

```powershell
& $MON --transport hdc --device $DEVICE collect --test-id MC-CPU-0831 --verify-hashes
& $MON --transport hdc --device $DEVICE collect --test-id MC-GPU-0831 --verify-hashes
```

通过条件：

- 本地存在对应 test/attempt 的 `events.jsonl`、`workload.log`、`final.json` 和 `artifact-hashes.json`；
- 哈希核验通过；
- 输出 `remote_removed=false`，设备目录仍存在；
- 重复拉取不会造成 test ID 串档。

## 6. 独立 telemetry 验收

telemetry 不是最小闭环的前置条件，但应证明它能独立工作，也能伴随 workload。

### TEL-01 独立采样

```powershell
& $MON --transport hdc --device $DEVICE telemetry run --profile cpu_stress_kirin9030 --test-id TEL-0831 --attempt-id TEL-0831-001 --duration 30 --interval 5
& $MON --transport hdc --device $DEVICE collect --test-id TEL-0831
```

通过条件：设备 `spool/telemetry.jsonl` 追加至少一个合法 JSON 对象；对象包含同一 test/attempt ID；期间不启动 workload、不向 UART 输出。

### TEL-02 伴随 workload

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART run --profile cpu_stress_kirin9030 --test-id TEL-RUN-0831 --attempt-id TEL-RUN-0831-001 --telemetry
& $MON --transport hdc --device $DEVICE collect --test-id TEL-RUN-0831
```

通过条件：核心 UART 仍完成 PASS/FAIL 判定，telemetry 只出现在设备本地文件，不穿插到 UART 事件流。

## 7. baseline 校验（非最小闭环）

只有需要 checksum/golden/阈值比较时才执行：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL run --profile cpu_mixed_big4 --baseline '<approved-baseline-id>' --test-id BASELINE-CPU-0831
```

通过条件：`validation_mode=baseline`，结果记录 baseline ID 和比较结果。baseline 缺失、未批准或与 profile 指纹不一致应在启动 workload 前失败。

生成流程只能消费显式运行目录：

```powershell
& $MON golden cpu --profile cpu_mixed_big4 --board-id BOARD-A --known-good --runs 2 --run-dir '<run1>' --run-dir '<run2>'
& $MON calibrate cpu --profile cpu_mixed_big4 --board-id BOARD-A --golden '<golden.json>' --runs 2 --run-dir '<run1>' --run-dir '<run2>'
& $MON baseline approve '<baseline-id>' --approver '<name>'
```

样本不够时 `calibrate` 必须失败，不能自行启动设备补样本。

## 8. 不修改设备环境的审计

在 PRE 和 MC 前后分别读取并保存以下状态（路径按平台 capability 调整）：

- CPU/GPU governor；
- scaling min/max/current frequency；
- CPU online；
- GPU power policy；
- workload affinity（如外部调度器设置）。

通过条件：Monitor 的 `run`、`smoke`、`telemetry run` 前后没有写入导致的变化；agent 代码中不存在这些 sysfs 的写操作。自然调频导致的 current frequency 变化不算写策略。

未来需要固定频率或 online 状态时，应由独立调度模块在测试前设置、审计并恢复；不能重新塞回 agent 或 `run`。

## 9. 现场问题定位

### run 卡住或零事件

按以下顺序检查：

1. 查看 PC `result.json` 中的 `attempt_id` 和 `device_evidence`。
2. 用 `collect --test-id ... --attempt-id ...` 拉设备证据。
3. `events.jsonl` 有 agent 事件但 PC 无事件：检查 UART 两端端口、波特率和独占占用。
4. `events.jsonl` 为空：检查 agent 是否启动、已部署路径和 shell 退出信息。
5. 有 `agent_start` 无 workload 事件：看 `workload.log`、workload 路径/权限和配置。
6. 有事件无 `agent_final`：看 timeout、workload 是否退出以及 agent 最终文件。

不要先把网络或 GitHub 波动当作核心链路问题；运行判定依赖的是设备 shell、UART 和本地证据。

### telemetry 无数据

先运行 `verify-deployment --profile ...`，再检查部署的 telemetry plan 和各候选只读路径。可选 metric 缺失不应阻止核心 run；显式启用 telemetry 而 collector 本身无法启动才是基础设施错误。

## 10. 验收记录模板

| ID | 命令退出码 | verdict/结果 | test_id/attempt_id | 证据路径 | 结论 |
|---|---:|---|---|---|---|
| PRE-01 | | | | | |
| PRE-02 | | | | | |
| PRE-03 CPU/GPU | | | | | |
| PRE-04 CPU/GPU | | | | | |
| MC-01 | | | | | |
| MC-02 | | | | | |
| MC-03 | | | | | |
| MC-04 | | | | | |
| MC-05 | | | | | |
| TEL-01 | | | | | |
| TEL-02 | | | | | |

最终判定：PRE-01 至 PRE-04 是环境准备完成；MC-01 至 MC-05 全部通过是最小闭环完成；TEL、baseline 和报告生成按项目需要独立验收。
