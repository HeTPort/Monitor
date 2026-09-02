# Monitor 用户指南

版本 2.1，更新于 2026-09-02。

## 1. 先理解命令边界

Monitor 把一次性准备和每次测试分开：

| 阶段 | 命令 | 作用 |
|---|---|---|
| 本地检查 | `validate` | 检查配置、profile、baseline 和打包资源 |
| 平台检查 | `probe` | 只读探测平台身份和能力 |
| Relay/ABI 检查 | `relay probe` | 单独读取设备 ABI，并测试已部署 relay 的运行能力 |
| 串口配对 | `pair` | 保存设备 UART 与 PC 串口的对应关系 |
| 部署 | `deploy` | 将 agent、workload、配置和 telemetry plan 部署到设备 |
| 部署核验 | `verify-deployment` | 只读核对已部署文件的哈希 |
| 核心测试 | `run` | 启动已部署 agent/workload，接收 UART 事件并判定 |
| 短测试 | `smoke` | `run` 的短 profile、无 baseline 别名 |
| 独立遥测 | `telemetry run` | 不启动 workload、不占用 UART，只向设备文件追加采样 |
| 证据拉取 | `collect` | 按 test ID 拉取设备本地日志，默认保留设备文件 |
| 资格化 | `golden` / `calibrate` / `baseline` | 从已准备设备实时采集或消费完整合格运行，生成并管理基线 |
| 报告 | `report` | 从已有 PC `result.json` 生成 markdown/json/csv |

`run` 不会隐式调用 `probe`、`deploy` 或 `verify-deployment`，也不会修改或恢复 governor、频率、CPU online、功耗策略和绑核状态。

## 2. 命令写法

以下示例假设使用打包后的 `monitor.exe`。从源码运行时，把 `$MON` 替换为 `python main.py`。全局参数必须写在子命令前。

```powershell
$MON = '.\dist\monitor.exe'
$DEVICE = '<HDC设备序列号>'
$PC_SERIAL = 'COM4'
$DEVICE_UART = '/dev/ttyHW0'
```

公共连接参数示例：

```powershell
& $MON --transport hdc --device $DEVICE '<子命令>'
```

如果已经成功执行 `pair`，`run` 可以复用保存的串口关系；也可以每次显式提供：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART '<子命令>'
```

## 3. 每个平台/BSP 只做一次的准备

### 3.1 本地检查

```powershell
& $MON validate --package
```

`--package` 会检查 workload、agent 和 shader 等发布资源。源码树没有放入真实板端二进制时，此项失败是资源尚未就绪，不代表 `run` 编排代码失败。

### 3.2 只读平台探测

```powershell
& $MON --transport hdc --device $DEVICE probe --platform kirin9030 --full
```

探测结果用于确认平台身份、sysfs 路径和 telemetry 能力。缺少可选路径应记录为 capability 缺失；平台身份无法可靠确认时必须失败关闭。探测不会写 sysfs。

### 3.3 串口配对

已知两端端口时建议显式配对：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART pair --platform kirin9030 --verify
```

### 3.4 Relay ABI 与构建

在公司设备上先执行（未部署 relay 时返回“不支持”是预期的，但会给出 ABI 字段）：

```powershell
& $MON --transport hdc --device $DEVICE --json relay probe --platform kirin9030
```

使用与 workload 相同的 OpenHarmony Clang target/sysroot/ABI 编译 `native/uart_relay/avs_uart_relay.c`，把产物放到 `config/platforms/kirin9030.yaml` 的 `serial.relay.local_asset`。不要把 PC Linux/Windows 编译产物复制到板端。

### 3.5 部署和核验

部署和核验以 profile 为单位。普通 CPU/GPU 压力和 CPU smoke 使用无 baseline 的 profile：

```powershell
& $MON --transport hdc --device $DEVICE deploy --profile cpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE deploy --profile gpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE deploy --profile cpu_smoke_kirin9030
& $MON --transport hdc --device $DEVICE verify-deployment --profile cpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE verify-deployment --profile gpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE verify-deployment --profile cpu_smoke_kirin9030
& $MON --transport hdc --device $DEVICE --device-uart $DEVICE_UART --json relay probe --platform kirin9030 --check-uart
```

部署内容不是只封装在 PC 端 exe 内。exe 内携带资源，`deploy` 明确地把资源释放并复制到设备的 `/data/local/tmp/avs`；之后 `run` 只调用已经部署的文件。一个 profile 的部署不会补齐另一个 profile 的专属 workload 配置和 telemetry plan。设备目录被清空或 run 更换 `--profile` 时，必须先对新 profile 执行 deploy/verify。这样部署失败与运行失败可以分别诊断。

## 4. 最小闭环：无 baseline 的错误判定

CPU：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART run --profile cpu_stress_kirin9030 --test-id 0831-CPU-01
```

GPU：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART run --profile gpu_stress_kirin9030 --test-id 0831-GPU-01
```

此时 `validation_mode` 为 `error-only`。agent 启动 workload，完整日志只追加到设备，UART 只传紧凑 START/HEARTBEAT/ERROR/SUMMARY/FINAL。原生 relay 使用 COBS+CRC32、完整写入和 `tcdrain()`；FIFO 结束后还会写入平台 `serial.tail_guard_bytes` 配置的 NUL 尾垫（Kirin9030 默认 64 字节），把 FINAL 尾部推出 UART/DMA。PC 在本次 START 前丢弃跨 run 残留，START 后严格校验 test/attempt ID、序号和 CRC，并且必须收到本次 FINAL。退出码为 0 且 verdict 为 `PASS` 即闭环通过。等待时间使用实际 `--baudrate` 和 frame/guard 大小，不以 9600 写死。

更新本功能后必须用 OpenHarmony toolchain 重新编译 relay、重新打包并执行 `deploy`；只替换 PC 端 exe 而保留设备上的 relay 1.0.0 不会获得尾部修复。`relay probe` 应显示 `avs-uart-relay 1.0.1`。agent/HDC 未能在运行结束时退出属于运行期基础设施错误，返回 3；PC 会主动终止仍在运行的 host transport，不再把它报成配置错误 4。

`smoke` 没有另一套执行逻辑，只是强制不使用 baseline；该兼容别名已弃用。新命令直接使用短 profile：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART run --profile cpu_smoke_kirin9030 --test-id 0831-SMOKE-01
```

CPU/GPU positive smoke 的 workload `verify_mode` 均为 `none`；它们验证快速 error-only 闭环，不是故障注入或 baseline 校验。checksum/golden-image 只用于显式 baseline profile。旧 `smoke` 命令暂时仍可调用并输出弃用提示，但不会生成 golden，也不会自动部署。`execute` 已删除。

## 5. test ID、attempt ID 和证据位置

- `test_id`：操作者定义的一组测试，例如 `0831-CPU-01`。
- `attempt_id`：一次具体尝试；未提供时自动生成。
- `--repeat N`：同一 test ID 下生成 N 个独立 attempt。
- `--attempt-id` 与 `--repeat` 在命令行中互斥。

设备证据位置：

```text
/data/local/tmp/avs/tests/<test_id>/<attempt_id>/
  events.jsonl
  workload.log
  workload-stderr.log
  workload-diagnostics.log
  relay.log
  final.json
  artifact-hashes.json
  spool/telemetry.jsonl       # 启用 telemetry 时
```

这些文件按 attempt 隔离，并以追加方式写入。PASS 或 FAIL 后都不会自动删除。

PC 默认使用 `--pc-artifacts result`，保留判定所需的紧凑结果。排查串口协议时使用：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL run --profile cpu_stress_kirin9030 --test-id 0831-DEBUG-01 --pc-artifacts full
```

`full` 额外保存原始串口和规范化事件；它不改变判定。

## 6. Telemetry 独立运行或伴随 workload

独立采样，不启动 workload、不写 UART：

```powershell
& $MON --transport hdc --device $DEVICE telemetry run --profile cpu_stress_kirin9030 --test-id 0831-TEL-01 --duration 60 --interval 5
```

伴随核心测试：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL run --profile cpu_stress_kirin9030 --test-id 0831-CPU-TEL-01 --telemetry
```

两种方式调用同一个 `avs-telemetry-agent` 和同一份 profile telemetry plan。采样只追加到设备本地 `spool/telemetry.jsonl`，不混入判错 UART。collector 在每个 metric/path 读取前后检查 stop-file 和 wall-clock deadline，采样间隔也可中断。伴随 workload 结束后，agent 只给 telemetry 有限的停止宽限期；超时会终止 collector、记录 `TELEMETRY_SHUTDOWN_TIMEOUT`，但仍继续写 `final.json` 并发送 `agent_final`。普通 error-only 运行不把 telemetry 缺失当作 DUT 错误；显式请求 `--telemetry` 但 collector 未部署属于基础设施错误。

## 7. 拉取和报告

拉取一个 test ID 的全部 attempts：

```powershell
& $MON --transport hdc --device $DEVICE collect --test-id 0831-CPU-01 --verify-hashes
```

只拉取一个 attempt：

```powershell
& $MON --transport hdc --device $DEVICE collect --test-id 0831-CPU-01 --attempt-id 0831-CPU-01-001 --verify-hashes
```

默认保留设备端数据。只有明确需要回收空间并且哈希核验成功时才删除：

```powershell
& $MON --transport hdc --device $DEVICE collect --test-id 0831-CPU-01 --verify-hashes --remove-remote-after-verify
```

已有 PC 运行目录可独立生成报告：

```powershell
& $MON report --run-dir '<运行目录>' --format markdown,json
```

## 8. 资格化与 baseline

普通压力测试只关心明确错误时，不传 `--baseline`。需要校验 checksum、golden 或阈值时才显式指定已批准 baseline：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL run --profile cpu_qualification_kirin9030 --baseline '<baseline-id>' --test-id QUAL-CPU-01
```

`golden` 有且只有两种输入方式：

- 不传任何 `--run-dir`：在已经 deploy/verify 的已知良品板上实时采集恰好 `--runs` 次，并自动拉取每次完整设备 attempt 目录；
- 传 `--run-dir`：必须恰好传 `--runs` 个，不允许传一部分后再隐式启动硬件补齐。

命令 JSON 输出中的 `source_runs` 是后续 `calibrate` 可直接复用的 PC 运行目录。每个目录可包含 PC `result.json`，以及 sibling `device-evidence/<attempt>/spool`；也可直接传已 collect 的 `spool`。资格化优先从设备 `workload.log` 的原生 summary 读取完整性能指标，UART 上的紧凑 summary 不需要扩大。

live capture 中，命令的 `--qualification-id` 同时作为设备和 PC 的顶层 `test_id`；每一次运行或重试生成新的 `attempt_id`，不会覆盖已有证据。因此失败后可直接使用命令 JSON 返回的两个 ID 执行：

```powershell
& $MON --transport hdc --device $DEVICE collect --test-id '<qualification-id>' --attempt-id '<returned-attempt-id>' --verify-hashes
```

Monitor 对 live golden 区分四个 deadline：设备 workload guard、收到 summary 前的 heartbeat 静默窗口、summary 后等待 `agent_final` 的短窗口，以及整体上限。以当前 CPU JSON 的 `timeout=75` 为例，分别为 80、90、20 和 300 秒。普通 `run` 仍严格使用 45 秒 heartbeat；这个放宽只适用于 `--generate-golden` 的已知同步计算阶段。workload 超过 80 秒会由设备 agent 报 `WORKLOAD_DEADLINE_EXCEEDED`，summary 后超过 20 秒没有 FINAL 会报 `AGENT_FINAL_TIMEOUT`，不会无限等待。

live golden 失败仍会尽力自动拉取设备 evidence，并在 JSON 中返回 `test_id`、`attempt_id`、PC `result_path`、设备 evidence 路径、原始 verdict/reason 以及附加 transport 状态。UART/策略结果是主错误；HDC 因前述错误被取消只作为次级基础设施原因，不再遮蔽原始超时。外部 workload 最终仍应在 golden 主计算阶段持续发 heartbeat，并自行执行它声明的 deadline；Monitor 的动态窗口负责兼容和防误杀，而不是把无界计算当成成功。

### 8.1 两块板的功能验收（快速证明数据链）

先分别在 BOARD-A 和 BOARD-B 上部署并核验 `cpu_qualification_kirin9030`，每块板实时采集一次 golden。保存两次输出里的 `source_runs[0]`，再做跨板一致性 golden 和最小两样本校准：

```powershell
& $MON --transport hdc --device '<BOARD-A设备>' --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json golden cpu --profile cpu_qualification_kirin9030 --board-id BOARD-A --known-good --runs 1 --qualification-id CPU-A-$SESSION
& $MON --transport hdc --device '<BOARD-B设备>' --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json golden cpu --profile cpu_qualification_kirin9030 --board-id BOARD-B --known-good --runs 1 --qualification-id CPU-B-$SESSION

& $MON --json golden cpu --profile cpu_qualification_kirin9030 --board-id BOARD-A --known-good --runs 2 --qualification-id CPU-2BOARD-$SESSION --run-dir 'BOARD-A=<A-source-run>' --run-dir 'BOARD-B=<B-source-run>'
& $MON --json calibrate cpu --profile cpu_qualification_kirin9030 --board-id BOARD-A --golden '<CPU-2BOARD golden_manifest>' --runs 2 --min-accepted 2 --baseline-id CPU-FUNCTIONAL-$SESSION --run-dir 'BOARD-A=<A-source-run>' --run-dir 'BOARD-B=<B-source-run>'
& $MON baseline approve "CPU-FUNCTIONAL-$SESSION" --approver '<name>'
```

这只证明资格化数据链可执行：两个样本均有 PASS、完整 telemetry、无 throttling、温度在指定范围内，并包含 CPU `operations_per_sec_avg`/`batch_time_ms_p99`（GPU 为 `fps_avg`/`frame_time_p99_ms`）。它不是生产阈值基线。

### 8.2 生产基线

生产流程仍使用 `config/policies/calibration.yaml`：至少 20 个被接受样本、至少 2 块板；建议按项目策略在每块板上采足重复数。先收集全部来源运行，再一次性执行 `golden` 和 `calibrate`。生产流程不要使用 `--min-accepted 2`。`calibrate` 只创建 draft，必须审阅 `proposed-baseline.json` 后人工 `baseline approve`。

GPU 使用同样流程，但换成 `gpu_qualification_kirin9030` 和 `golden gpu`/`calibrate gpu`；每次 golden run 还必须有内容完全一致的 `gpu-golden.rgba`。

批准后，使用同一个 state 目录显式部署、核验并运行：

```powershell
& $MON --transport hdc --device $DEVICE deploy --profile cpu_qualification_kirin9030 --baseline '<approved-baseline-id>'
& $MON --transport hdc --device $DEVICE verify-deployment --profile cpu_qualification_kirin9030 --baseline '<approved-baseline-id>'
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART run --profile cpu_qualification_kirin9030 --baseline '<approved-baseline-id>' --test-id "QUAL-CPU-$SESSION"
```

## 9. UART v2 诊断接口

`simulate` 是离线判定接口。JSONL 事件可选择 `--realtime` 节奏重放；真实 `serial.raw` 使用 UART v2 会话发现、COBS、CRC、身份与序号校验，固定做确定性重放：

```powershell
& $MON --json simulate --events '<run>/events.jsonl' --profile cpu_stress_kirin9030
& $MON --json simulate --raw-serial '<run>/serial.raw' --profile cpu_stress_kirin9030
```

两种输入应复现原运行的 verdict/exit code；`--realtime` 不能和 `--raw-serial` 同用。每次重放分配唯一 `replay_id`，写入 `output/simulations/<replay-id>/<original-test-id>/<original-attempt-id>`，并记录输入路径与 SHA-256；UART 身份仍按原 test/attempt 校验，但不会写回或覆盖 live 运行目录。`monitor` 直接从 `$PC_SERIAL` 发现并解码一段 UART v2 会话，可保存 raw 证据，但因为没有完整 run manifest，只输出诊断结果 `NOT_EVALUATED`，不能替代 `run`：

```powershell
& $MON --pc-serial $PC_SERIAL --baudrate 115200 --json monitor --save-raw --timeout 60
```

`monitor` 和 `run` 必须独占串口，不能同时打开同一个 COM 口。

## 10. 配置边界

平台依赖写入配置，不写死在运行逻辑中：

- `config/platforms/<platform>.yaml`：平台身份、UART 候选、CPU/GPU/thermal 只读路径。
- `config/profiles/<profile>.yaml`：workload、telemetry 和 `scheduler_requirements`。
- `config/workloads/<workload>.json`：workload 参数及 `verify_mode`。

新增平台时复制 profile/workload 数据文件并修改平台 ID、能力、路径和 workload 参数，不在 Python 中增加平台分支。普通 stress/smoke 使用 `verify_mode: none`；CPU 资格化使用 `checksum`，GPU 资格化使用 `golden-image`，且 `golden_file` 必须是设备根目录下的绝对路径。`validate --profile ...` 会解析并校验这些 workload 字段，不再只检查文件是否存在。

`scheduler_requirements` 当前只是声明性元数据。Monitor 2.1 不执行它。未来调度模块若要设置 governor、频率、CPU online、功耗策略或 affinity，必须有独立命令、权限、审计和恢复策略。

## 11. 判定和退出码

| 退出码 | 含义 |
|---:|---|
| 0 | PASS |
| 1 | DUT 明确失败 |
| 2 | workload 无有效结论（silent failure） |
| 3 | 基础设施、agent、串口或事件协议错误 |
| 4 | 配置、profile 或 baseline 错误 |
| 5 | 平台/能力不支持 |
| 6 | 用户中止 |

常见定位顺序：先看 PC `result.json`，再按其中的 `device_evidence` 使用 `collect` 拉取 `events.jsonl`、`workload.log`、`final.json` 和 telemetry。不要先归因于网络；核心 UART 判定与设备本地证据不依赖 GitHub。
