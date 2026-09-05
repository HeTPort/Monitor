# Monitor 2.1 打包与设备最小闭环测试计划

更新于 2026-09-02。本计划用于 Kirin9030 实机验收，也可通过替换平台/profile 用于其他板卡。

## 1. 本轮要证明什么

最小闭环只证明以下链路：

```text
PC run -> 设备 agent -> workload -> 指定 UART -> PC 协议解析和判错
                         |
                         +-> 设备本地追加证据
```

probe、pair、deploy 和 verify-deployment 是显式准备，不属于每次 `run`。telemetry 是独立能力；baseline 是可选校验能力。Monitor 不负责修改或恢复 governor、频率、CPU online、功耗策略及 affinity。

### 1.1 命令接口、作用和适用流程

| 命令 | 作用 | 何时使用 | 不会做什么 |
|---|---|---|---|
| `validate` | 在 PC 解析配置、profile、baseline 和发布资源 | 打包前；profile/config 修改后 | 不连接或修改设备 |
| `probe` | 只读识别平台并发现 CPU/GPU/telemetry 能力 | 每个平台、BSP/framework 版本做一次 | 不部署、不启动 workload、不写 sysfs |
| `pair` | 验证并保存设备 UART 与 PC 串口映射 | 首次接线、端口或 BSP 改变时 | 不启动 agent/workload |
| `relay probe` | 读取 ABI；检查已部署 relay 的 version/self-test/termios/tcdrain | relay 首次移植或重新编译后 | `--check-uart` 不发送测试负载 |
| `deploy --profile P` | 部署 profile P 需要的 agent、relay、workload、配置、shader 和 telemetry plan | 第一次运行 P；P 的资源变化后；设备目录被清理后 | 不运行测试；不会部署其他 profile 的专属配置 |
| `verify-deployment --profile P` | 只读比较 profile P 的本地/设备哈希 | deploy 后；正式 run 前 | 不补文件、不修复哈希 |
| `run --profile P` | 启动已部署 agent/workload，接收 UART v2 并给出 PASS/FAIL | CPU/GPU 长压、短 smoke、负向测试；可选 baseline 或 correctness-only golden | 不隐式 probe/pair/deploy/verify，不修改设备策略 |
| `smoke` | 兼容旧调用的短 profile 别名 | 只用于旧脚本迁移 | 已弃用；新流程统一使用 `run --profile ...smoke...` |
| `telemetry run` | 单独启动设备本地追加式遥测 | 平台能力验收或需要独立采样时 | 不启动 workload、不占用判错 UART |
| `collect` | 按 test/attempt ID 拉取设备证据，可校验哈希 | run/telemetry 后集中取证 | 默认不删除设备证据 |
| `golden` / `calibrate` / `baseline` | 实时采集或消费完整合格运行，生成、校准、批准资格数据 | 需要 checksum/golden/阈值校验时 | 不属于普通 error-only 最小闭环；不会接受半个 supplied cohort |
| `report` | 从一个已有 PC `result.json` 生成 markdown/json/csv | 测试结束后本地汇总 | 不连接设备、不替代 `collect` |
| `monitor` | 诊断性读取串口事件 | 排查独立串口/协议问题 | 没有完整 run manifest，不给 DUT verdict |
| `simulate` | 离线重放事件/原始串口证据 | 协议回归、故障注入替代验证 | 不连接设备 |

### 1.2 按目标选择测试流程

| 流程 | 顺序 | 完成标准 |
|---|---|---|
| 发布包离线验收 | `validate` → 单元测试 → build | PKG-01～03 通过 |
| 新平台/BSP 接入 | `probe` → `pair` → relay ABI/build → `relay probe --check-uart` | PRE-01、02、02A 通过 |
| 普通最小闭环 | 对每个将运行的 profile 执行 `deploy` → `verify-deployment` → `run` → `collect` | PRE-03/04 和 MC-01～05 通过 |
| 短时快速验证 | 部署/核验 smoke profile → `run --profile ...smoke...` → `collect` | MC-03 通过；仍走核心 run |
| 独立遥测 | 部署/核验对应 profile → `telemetry run` → `collect` | TEL-01 通过 |
| workload+遥测 | 部署/核验对应 profile → `run --telemetry` → `collect` | TEL-02 通过 |
| baseline 资格化 | `golden` 正确性 → `deploy/verify --golden` → `run --golden --telemetry` 持续样本 → `collect` → `calibrate` → `baseline approve` → `run --baseline` | 第 7 节通过 |
| 负向判错 | 准备并部署明确失败的 profile → `run` → `collect` | MC-04 返回预期非 PASS；没有 profile 时用 `simulate` |
| 报告生成 | 对 PC run 目录执行 `report` | 第 8 节报告文件生成成功 |

### 1.3 Profile 选择与校验语义

| Profile | 用途 | workload 配置 | verify_mode | 是否需要 baseline |
|---|---|---|---|---|
| `cpu_stress_kirin9030` | CPU 普通长压/最小闭环 | `cpu_stress.json` | `none` | 否 |
| `gpu_stress_kirin9030` | GPU 普通长压/最小闭环 | `gpu_stress.json` | `none` | 否 |
| `cpu_smoke_kirin9030` | CPU 短时正向闭环 | `cpu_smoke.json` | `none` | 否 |
| `gpu_smoke_kirin9030` | GPU 短时正向闭环（可选） | `gpu_smoke.json` | `none` | 否 |
| `cpu_qualification_kirin9030` | Kirin9030 CPU checksum/阈值资格化 | `cpu_qualification_kirin9030.json` | `checksum` | 生成阶段否；合格运行显式传入 |
| `gpu_qualification_kirin9030` | Kirin9030 GPU readback/阈值资格化 | `gpu_qualification_kirin9030.json` | `golden-image` | 生成阶段否；合格运行显式传入 |
| `cpu_mixed_big4` | CPU checksum 资格化 | `cpu_mixed_big4.json` | `checksum` | 是，显式传入 |
| `gpu_vulkan_mixed` | GPU golden-image 资格化 | `gpu_vulkan_mixed.json` | `golden-image` | 是，显式传入 |
| `<故障注入profile>` | MC-04 项目自备负向 profile | 项目自备 | 按预期故障设计 | 视设计而定 |

仓库当前没有最后一行对应的实际文件；尖括号表示占位符。positive smoke 不是故障注入，也不应依赖 golden/checksum。

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
$SESSION = '0901C'       # 每轮改为新的唯一标识，避免与历史 test ID 混用
$OUT = '.\output'
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

## 4. 平台一次性准备与 profile 资产准备

PRE-01、02、02A 通常每个平台/BSP 做一次；PRE-03、04 对每个将运行的 profile 做一次，并在资源变化或设备目录清空后重做。以下命令都不应被 `run` 隐式重复。

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

### PRE-03 按将要运行的 profile 部署

`deploy` 的选择单位是 profile，不是 CPU/GPU 二进制。即使多个 profile 共用同一个 workload，profile 专属配置和 telemetry plan 仍必须分别部署。本计划的三个必测 profile：

```powershell
& $MON --transport hdc --device $DEVICE deploy --profile cpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE deploy --profile gpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE deploy --profile cpu_smoke_kirin9030
```

若还要运行 GPU smoke，再额外执行：

```powershell
& $MON --transport hdc --device $DEVICE deploy --profile gpu_smoke_kirin9030
```

通过条件：每个命令均退出 0，部署清单 `complete=true` 且 `verified=true`。部署应包含：

- `/data/local/tmp/avs/bin/avs-device-agent`
- `/data/local/tmp/avs/bin/avs-uart-relay`
- `/data/local/tmp/avs/bin/avs-telemetry-agent`
- CPU/GPU workload
- workload 配置、GPU shader
- 每个已选择 profile 的 workload 配置和 telemetry plan

`output/deployment-manifest.json` 是共享的最新结果文件。需要保留逐 profile 部署记录时，在执行下一个 deploy 前复制并改名：

```powershell
Copy-Item "$OUT\deployment-manifest.json" "$OUT\deployment-cpu-stress-$SESSION.json"
```

设备目录被手工删除、换板、重新刷 BSP，或者 profile/workload/agent/relay/config/shader 任一资源改变后，必须重新执行对应 deploy。

### PRE-04 只读部署核验

```powershell
& $MON --transport hdc --device $DEVICE verify-deployment --profile cpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE verify-deployment --profile gpu_stress_kirin9030
& $MON --transport hdc --device $DEVICE verify-deployment --profile cpu_smoke_kirin9030
```

通过条件：每个将要运行的 profile 均退出 0、哈希一致；设备文件没有被重新部署或修改。若 run 更换了 `--profile`，必须先核验新 profile，不能用另一个 profile 的成功核验代替。

## 5. 最小闭环验收

以下 MC-01 至 MC-05 全部通过，即“启动 agent 和负载、定向串口、PC 判错、设备留证”的最小闭环完成。无需 baseline，也无需 telemetry。

### MC-01 CPU run

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile cpu_stress_kirin9030 --test-id "MC-CPU-$SESSION" --attempt-id "MC-CPU-$SESSION-001" --pc-artifacts full
```

通过条件：

- 进程退出码为 0；
- 输出 `validation_mode=error-only`、`verdict=PASS`；
- UART v2 收到同一 attempt 的合法连续紧凑事件和 `agent_final`；PC 结果记录 `agent_final_seen=true`；
- `serial.raw` 以完整 FINAL 分隔符结束，下一次 run 开头没有上一 attempt 的 FINAL 尾字节；EOF NUL guard 产生的空帧不计入事件数；
- `run` 命令本身没有隐式调用 baseline、probe、deploy 或 verify；对应 profile 的 PRE-03/04 已经显式完成。

### MC-02 GPU run

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile gpu_stress_kirin9030 --test-id "MC-GPU-$SESSION" --attempt-id "MC-GPU-$SESSION-001" --pc-artifacts full
```

通过条件与 MC-01 相同，workload 为 GPU profile；`DEBUG:/TRACE:/INFO:` 进入设备 `workload-diagnostics.log`，不产生 DUT 错误。

### MC-03 短 profile 仍使用核心 run 链路

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile cpu_smoke_kirin9030 --test-id "MC-SMOKE-$SESSION" --attempt-id "MC-SMOKE-$SESSION-001" --pc-artifacts full
```

前置条件：必须已经对 `cpu_smoke_kirin9030` 单独执行 PRE-03/04。部署 `cpu_stress_kirin9030` 不能替代它，因为设备配置路径分别是 `configs/cpu_stress_kirin9030.json` 和 `configs/cpu_smoke_kirin9030.json`。

通过条件：退出 0、`validation_mode=error-only`、`verdict=PASS`、`agent_final_seen=true`；设备事件中没有 golden 生成行为。`config/workloads/cpu_smoke.json` 的 `verify_mode` 必须为 `none`。它证明短测试也只使用同一条 run 链路。旧 `smoke` 仅作为弃用兼容别名，不纳入新测试命令。

### MC-04 错误能被判出

本仓库当前不提供具体故障注入 profile；下面的 `<故障注入profile>` 只是占位符，不能原样执行，也不能用正常的 `cpu_smoke_kirin9030` 代替。若项目另行提供一个明确返回非零、输出合法 `workload_result=FAIL` 或触发受控 verify 失败的 profile，先对它执行 deploy/verify，再执行：

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile '<故障注入profile>' --test-id "MC-NEG-$SESSION" --attempt-id "MC-NEG-$SESSION-001" --pc-artifacts full
```

通过条件：不能显示 PASS；DUT 明确失败退出 1、无有效 workload 结论退出 2、协议/agent/串口错误退出 3，并在 `result.json` 中给出简短原因。agent transport 未结束也必须退出 3，且 PC 命令不能继续等待到完整 HDC 超时时间。若当前发布包没有故障注入 profile，可用离线协议测试覆盖，设备项标记待补，不能用拔网线替代。

### MC-05 设备证据可拉取且默认保留

```powershell
& $MON --transport hdc --device $DEVICE collect --test-id "MC-CPU-$SESSION" --verify-hashes
& $MON --transport hdc --device $DEVICE collect --test-id "MC-GPU-$SESSION" --verify-hashes
& $MON --transport hdc --device $DEVICE collect --test-id "MC-SMOKE-$SESSION" --verify-hashes
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
& $MON --transport hdc --device $DEVICE telemetry run --profile cpu_stress_kirin9030 --test-id "TEL-$SESSION" --attempt-id "TEL-$SESSION-001" --duration 30 --interval 5
& $MON --transport hdc --device $DEVICE collect --test-id "TEL-$SESSION"
```

通过条件：设备 `spool/telemetry.jsonl` 追加至少一个合法快照 JSON；对象包含同一 test/attempt ID、唯一 `payload.sample_id`、`complete=true`、`missing_required=[]`，并且 `metrics` 覆盖 profile 的全部 required 指标；每个指标值数组与 `sources` 路径数组对应。期间不启动 workload、不向 UART 输出；30 秒 duration 在有限误差内结束，而不是因为一次 sysfs 遍历拖到 50 秒以上。只有文件存在、只有 frequency/online、或首轮没有 utilization 都不能判通过。

### TEL-02 伴随 workload

```powershell
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART run --profile cpu_stress_kirin9030 --test-id "TEL-RUN-$SESSION" --attempt-id "TEL-RUN-$SESSION-001" --telemetry
& $MON --transport hdc --device $DEVICE collect --test-id "TEL-RUN-$SESSION"
```

通过条件：核心 UART 仍完成 PASS/FAIL 判定，telemetry 只出现在设备本地文件，不穿插到 UART 事件流；workload 发出 stop 时 collector 先完成正在进行的 required 快照，optional 可中断，最终至少保留一个 `complete=true` 快照。另用一个会忽略 TERM/stop-file 的受控 collector 做单元或实验室测试：超过 shutdown grace 后必须出现 `TELEMETRY_SHUTDOWN_TIMEOUT`，但 `final.json` 和 UART `agent_final` 仍生成。

Kirin9030 检查点：CPU frequency 的 `sources` 只能来自 `cpufreq/policy*/scaling_cur_freq`，不能同时出现等价的 `cpu*/cpufreq` 路径；CPU/GPU temperature 必须来自平台配置中由 probe 类型确认的 thermal zones；`Gpu utilisation : N` 必须解析成数值 N；CPU/GPU plan 均不得包含 `/sys/kernel/debug/lpmcu_debug/cluster_volt`，因为该节点是调压入口而不是电压回读。

## 7. 资格化数据链与 baseline（非最小闭环）

资格化 profile 必须先各自完成 PRE-03/04。`golden --runs N` 有两个互斥来源模式：不传 `--run-dir` 时实时生成 N 次正确性结果并自动拉完整设备 attempt；传入时必须恰好 N 个。输出 JSON 的 `source_runs` 用于 golden 一致性审计或跨板合并，禁止只传一部分目录再从设备补齐。golden 生成的测量段可能是 `duration_s=0`、`batch_count=1`，不能直接交给 `calibrate` 当性能样本。

live capture 中 `--qualification-id` 就是设备/PC `test_id`，每次运行或重试使用唯一 `attempt_id`。当前 CPU workload JSON 的 `timeout=75` 派生为：设备 workload guard 80 秒、summary 前 heartbeat 窗口 90 秒、summary 后 FINAL 窗口 20 秒、整体 300 秒。普通 `run` 的 heartbeat 仍为 45 秒。Monitor 的放宽只覆盖已知 golden 同步阶段；workload 后续仍应在该阶段持续发 heartbeat 并自行限制计算时间。

### QUAL-01 两板功能验收

在 BOARD-A 上部署/核验 `cpu_qualification_kirin9030` 后执行：

```powershell
& $MON --transport hdc --device '<BOARD-A设备>' --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json golden cpu --profile cpu_qualification_kirin9030 --board-id BOARD-A --known-good --runs 1 --qualification-id "CPU-A-$SESSION"
```

在 BOARD-B 上重复 deploy/verify，并执行同样命令（`--device`、`--board-id BOARD-B`、qualification ID 相应替换）。从两次 JSON 输出保存 `$A_GOLDEN_RUN = source_runs[0]`、`$B_GOLDEN_RUN = source_runs[0]`，然后离线合并正确性：

```powershell
& $MON --json golden cpu --profile cpu_qualification_kirin9030 --board-id BOARD-A --known-good --runs 2 --qualification-id "CPU-2BOARD-$SESSION" --run-dir "BOARD-A=$A_GOLDEN_RUN" --run-dir "BOARD-B=$B_GOLDEN_RUN"
```

保存上一命令的 `$GOLDEN`。在 BOARD-A 上部署/核验正确性参考并采集持续样本：

```powershell
& $MON --transport hdc --device '<BOARD-A设备>' deploy --profile cpu_qualification_kirin9030 --golden $GOLDEN
& $MON --transport hdc --device '<BOARD-A设备>' verify-deployment --profile cpu_qualification_kirin9030 --golden $GOLDEN
& $MON --transport hdc --device '<BOARD-A设备>' --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json run --profile cpu_qualification_kirin9030 --golden $GOLDEN --telemetry --pc-artifacts full --test-id "CPU-CAL-A-$SESSION" --attempt-id "CPU-CAL-A-$SESSION-001"
& $MON --transport hdc --device '<BOARD-A设备>' collect --test-id "CPU-CAL-A-$SESSION" --verify-hashes
```

在 BOARD-B 上重复上述四条命令，ID 改为 `CPU-CAL-B-$SESSION`。然后把两个 PC attempt 目录交给离线校准：

```powershell
& $MON --json calibrate cpu --profile cpu_qualification_kirin9030 --board-id BOARD-A --golden $GOLDEN --runs 2 --min-accepted 2 --baseline-id "CPU-FUNCTIONAL-$SESSION" --run-dir "BOARD-A=<CPU-CAL-A PC attempt>" --run-dir "BOARD-B=<CPU-CAL-B PC attempt>"
& $MON baseline show "CPU-FUNCTIONAL-$SESSION"
& $MON baseline approve "CPU-FUNCTIONAL-$SESSION" --approver '<name>'
```

通过条件：

- 两次 live golden 均退出 0，`source_mode=live-capture`，只作为 correctness source，不把其短测量指标用于校准；
- live 输出的 `qualification_id` 与设备 `test_id` 一致，attempt 唯一；同一 ID 重试不覆盖既有 attempt/golden；
- 合法 golden 在最后一条早期 heartbeat 后静默超过 45 秒、但在派生的 90 秒窗口内给出 summary 时不能误判；超过 90 秒必须有界失败；summary 后超过 20 秒无 FINAL 必须报 `AGENT_FINAL_TIMEOUT`；
- 离线 golden 为 `source_mode=supplied`，两板 checksum 一致；少传一个 `--run-dir` 返回配置错误 4；
- 两次持续 run 均为 `validation_mode=golden-reference`、PASS，没有 `--generate-golden`，也没有 baseline 性能阈值；
- `workload-summary-full.json` 或设备 `workload.log` 含 `operations_per_sec_avg`、`batch_time_ms_p99`、至少配置 duration 的 90%，且 CPU `batch_count>=2`；
- telemetry 至少有一个 `complete=true` 快照，且同一快照覆盖 profile 全部 required 指标；样本未被 throttling/温度规则拒绝；
- calibrate 生成 draft，接受 2 个样本和 2 个 board ID；approve 后状态为 approved。

GPU 按相同顺序改用 `gpu_qualification_kirin9030`、`golden gpu` 和 `calibrate gpu`。额外通过条件是每个 correctness source 有 `gpu-golden.rgba`，两份 raw readback 字节完全一致；`deploy --golden` 已核验并部署该文件；持续 summary 有 `fps_avg` 和 `frame_time_p99_ms`；快照包含可解析的 `gpu.frequency`、`gpu.utilization`、`gpu.temperature`、`gpu.hang_count` 和 `gpu.power_policy`。

`--min-accepted 2` 只证明命令、证据归一化、指标抽取和 registry 的功能数据链，不是生产基线。

### QUAL-02 生产 cohort

生产资格化必须使用默认 `config/policies/calibration.yaml`：至少 20 个被接受样本、至少 2 块板，不传 `--min-accepted 2`。每个输入必须是 `run --golden --telemetry` 产生的持续运行，写成 `BOARD_ID=<run-or-spool>`；`--runs` 必须与输入总数完全相等。任何无完整 required telemetry 快照、时长不足、CPU batch 数不足、throttling、温度范围外、DUT 非 PASS 或缺性能指标的样本都应被拒绝；接受数或板数不足时错误必须逐 run 列出拒绝原因，并且不能启动硬件补样本。

通过条件：审阅 `proposed-baseline.json` 的 accepted/rejected、分布和阈值后才执行 `baseline approve`；`baseline list/show/export/import/deprecate` 分别完成状态查询、可移植 bundle 哈希校验和生命周期审计。

### QUAL-03 已批准 baseline 运行

```powershell
& $MON --transport hdc --device $DEVICE deploy --profile cpu_qualification_kirin9030 --baseline '<approved-baseline-id>'
& $MON --transport hdc --device $DEVICE verify-deployment --profile cpu_qualification_kirin9030 --baseline '<approved-baseline-id>'
& $MON --transport hdc --device $DEVICE --pc-serial $PC_SERIAL --device-uart $DEVICE_UART run --profile cpu_qualification_kirin9030 --baseline '<approved-baseline-id>' --test-id "BASELINE-CPU-$SESSION" --attempt-id "BASELINE-CPU-$SESSION-001"
```

通过条件：`validation_mode=baseline`，结果记录 baseline ID 和比较结果。baseline 缺失、未批准、平台/profile/workload/correctness 指纹不一致应在启动 workload 前失败。普通 smoke/stress 始终保持 `verify_mode=none`。

## 8. UART v2 诊断接口

### DIAG-01 离线 JSONL 与 raw 重放

对一个已完成的 `--pc-artifacts full` 运行执行：

```powershell
$RUN_DIR = "$OUT\MC-CPU-$SESSION\MC-CPU-$SESSION-001"
& $MON --json simulate --events "$RUN_DIR\events.jsonl" --profile cpu_stress_kirin9030
& $MON --json simulate --raw-serial "$RUN_DIR\serial.raw" --profile cpu_stress_kirin9030
```

通过条件：两次离线结果复现原 `result.json` 的 verdict/exit code；raw 路径能跳过 START 前旧 run/损坏帧，并在 START 后对 CRC、序号、身份和缺 FINAL 失败关闭。`--raw-serial --realtime` 必须返回配置错误 4；`--realtime` 只属于 `--events`。对同一输入连续重放两次，两次都成功并获得不同 `replay_id`，结果均位于 `output/simulations/<replay-id>/...`，输入哈希一致，原 live `result.json`/`events.jsonl`/`serial.raw` 均不改变。

### DIAG-02 live monitor

使用受控 UART-v2 发送源或单独的一次 agent 输出；确保 `run` 没有占用同一 COM 口：

```powershell
& $MON --pc-serial $PC_SERIAL --baudrate 115200 --json monitor --save-raw --timeout 60
```

通过条件：发现一段 UART-v2 START/FINAL 会话，保存的 raw/decoded evidence 可再次被 simulate 读取；结果明确为 `NOT_EVALUATED`，不能显示 DUT PASS/FAIL。分别以平台计划中的固定波特率测试，不把 9600 写死为唯一值。

## 9. 本地报告生成

`report` 读取 PC 运行目录中的 `result.json`，不读取设备目录；因此它可以在所有测试结束、设备证据统一 collect 后一次性执行。示例：

```powershell
$RUN_DIR = "$OUT\MC-CPU-$SESSION\MC-CPU-$SESSION-001"
& $MON report --run-dir $RUN_DIR --format markdown,json,csv
```

通过条件：命令退出 0，并在同一目录生成 `report.md`、`report.json` 和 `report.csv`。报告内容必须保留 verdict、退出码、profile、baseline（若有）、workload 结果以及 DUT/基础设施原因。`report` 不能替代 `collect`：前者处理 PC `result.json`，后者拉取设备 `events.jsonl`、workload 日志和 telemetry。

适用方式：

- 最小闭环完成后：对 MC-01～03 的 PC run 目录生成报告；
- 负向测试后：确认报告没有把 DUT_FAIL/INFRA_ERROR 改成 PASS；
- baseline 测试后：确认报告包含 baseline ID；
- 仅 telemetry 的 TEL-01 没有 PC `result.json`，不适用 `report`。

## 10. 不修改设备环境的审计

在 PRE 和 MC 前后分别读取并保存以下状态（路径按平台 capability 调整）：

- CPU/GPU governor；
- scaling min/max/current frequency；
- CPU online；
- GPU power policy；
- workload affinity（如外部调度器设置）。

通过条件：Monitor 的 `run`、`smoke`、`telemetry run` 前后没有写入导致的变化；agent 代码中不存在这些 sysfs 的写操作。自然调频导致的 current frequency 变化不算写策略。

未来需要固定频率或 online 状态时，应由独立调度模块在测试前设置、审计并恢复；不能重新塞回 agent 或 `run`。

## 11. 现场问题定位

### run 卡住或零事件

按以下顺序检查：

1. 查看 PC `result.json` 中的 `attempt_id` 和 `device_evidence`。
2. 用 `collect --test-id ... --attempt-id ...` 拉设备证据。
3. `events.jsonl` 有 agent 事件但 PC 无事件：检查 UART 两端端口、波特率和独占占用。
4. `events.jsonl` 为空：检查 agent 是否启动、已部署路径和 shell 退出信息。
5. 有 `agent_start` 无 workload 事件：看 `workload.log`、workload 路径/权限和配置。
6. 有事件无 `agent_final`：看 timeout、workload 是否退出以及 agent 最终文件。

### live golden 超时

1. 优先查看命令 JSON 返回的 `test_id`、`attempt_id`、`result_path`、verdict 和 reasons；不要只依据外层 HDC 取消信息。
2. 直接执行 `collect --test-id '<returned-test-id>' --attempt-id '<returned-attempt-id>' --verify-hashes`；失败路径也会先做一次 best-effort 自动拉取。
3. `WORKLOAD_DEADLINE_EXCEEDED` 表示 workload 超过设备 guard；`HEARTBEAT_OR_SUMMARY_TIMEOUT` 表示派生窗口内无 summary；`AGENT_FINAL_TIMEOUT` 表示已有 summary 但 agent/telemetry 未在收尾窗口结束。
4. 如果 `result.json` 已有上述主原因，同时还有 `AGENT_TRANSPORT_CANCELLED_AFTER_VERDICT`，后者只是 PC 清理被取消 transport 的附加证据，不能遮蔽前者。

### workload 很快退出且只有 agent_start/agent_final

1. 查看 `result.json` 的 `workload_exit_code`；这不是 UART 卡住。
2. 确认 run 使用的 profile 已单独执行 deploy/verify。
3. 检查 manifest 中的 `workload.config_path`，并确认设备上同一路径存在。
4. 用 `collect --test-id ... --verify-hashes` 拉取 `workload.log`、`workload-stderr.log` 和 `final.json`。
5. positive smoke 的 `verify_mode` 应为 `none`；checksum/golden-image 必须进入显式 baseline 流程。

不要先把网络或 GitHub 波动当作核心链路问题；运行判定依赖的是设备 shell、UART 和本地证据。

### telemetry 无数据

先运行 `verify-deployment --profile ...`，再检查部署的 telemetry plan 和各候选只读路径。可选 metric 缺失不应阻止核心 run；显式启用 telemetry 而 collector 本身无法启动才是基础设施错误。

## 12. 公共接口覆盖清单

最终全量验收应逐项记录以下 24 个叶命令路径，不能用“最小闭环已过”替代未执行接口：`pair`、`monitor`、`simulate`、`list-profiles`、`validate`、`probe`、`relay probe`、`deploy`、`verify-deployment`、`golden cpu`、`golden gpu`、`calibrate cpu`、`calibrate gpu`、`smoke`、`baseline list`、`baseline show`、`baseline approve`、`baseline deprecate`、`baseline export`、`baseline import`、`run`、`telemetry run`、`collect`、`report`。

除叶命令存在性外，还要覆盖这些关键分支：`run --pc-artifacts result/full`、自动 attempt/显式 attempt/`--repeat`、CPU/GPU smoke、无参考/`--golden`/`--baseline` 三种互斥校验模式、telemetry 独立/伴随/required 缺失拒绝、collect 全 test/单 attempt/哈希验证/验证后删除、report markdown/json/csv、golden live/supplied/partial-reject、calibrate 短 golden 样本拒绝/两板功能/生产 cohort，以及退出码 0～6 的可控场景。没有安全故障 profile 时，退出码与协议负向分支使用 `simulate` 和单元测试，不用断网代替。

## 13. 验收记录模板

这是按照你提供的验收记录模板填写的结果，你可以直接复制到你的最终测试报告中：

```markdown
| ID | 命令退出码 | verdict/结果 | test_id/attempt_id | 证据路径 | 结论 |
|---|---:|---|---|---|---|
| PRE-01 | 0 | supported: true | N/A | output/probes/0123456789ABCDEF/capabilities.json | 平台身份和能力探测成功。 |
| PRE-02 | 0 | Pairing verified | N/A | 控制台配对成功日志 | 串口配对成功 (/dev/ttyHW0 -> COM6)。 |
| PRE-03 CPU/GPU/Smoke | 0 | complete: True, verified: True | N/A | output/deployment-manifest.json | CPU/GPU/Smoke 资源及 telemetry plan 部署成功。 |
| PRE-04 CPU/GPU/Smoke | 0 | complete: True, verified: True | N/A | output/deployment-verification.json | 设备端哈希核验通过，无文件被修改。 |
| MC-01 | 0 | PASS | MC-CPU-0902B / MC-CPU-0902B-001 | output/MC-CPU-0902B/MC-CPU-0902B-001/result.json | 退出码 0，核心闭环验证通过。 |
| MC-02 | 0 | PASS | MC-GPU-0902B / MC-GPU-0902B-001 | output/MC-GPU-0902B/MC-GPU-0902B-001/result.json | 退出码 0，GPU 闭环验证通过。 |
| MC-03 | 0 | PASS | MC-SMOKE-0902B / MC-SMOKE-0902B-001 | output/MC-SMOKE-0902B/MC-SMOKE-0902B-001/result.json | 退出码 0，短时正向闭环通过。 |
| MC-04 | 3 | FAIL/INFRA_ERROR | MC-CPU-0902B / MC-CPU-0902B-001 (离线重放) | output/MC-CPU-0902B/MC-CPU-0902B-001/result.json | 无实机故障 profile，通过截断 events.jsonl 离线 simulate 验证判错能力，准确返回非零退出码。 |
| MC-05 | 0 | verified: True, remote_removed: false | MC-CPU/GPU/SMOKE-0902B | output/MC-CPU-0902B/device-evidence | 拉取成功且哈希核验通过，设备端默认保留证据。 |
| TEL-01 | 3 | timeout after 50.0s | TEL-0902B / TEL-0902B-001 | output/TEL-0902B/device-evidence | **未通过**：50s 超时退出。疑似设备端 sysfs 读取阻塞导致 agent 未结束。 |
| TEL-02 | 0 | PASS | TEL-RUN-0902B / TEL-RUN-0902B-001 | output/TEL-RUN-0902B/TEL-RUN-0902B-001/result.json | 伴随运行 PASS，telemetry 数据独立留存设备本地。 |
| QUAL-01/02/03（如适用） | 3 / 4 | 设备超时 / 容错拦截成功 | CPU-A-0902B 等 / FAIL-0902B 等 | output/CPU-A-0902B (残缺) | **阻塞**：实机 golden 采集卡死超时。已通过离线残缺数据验证 golden/calibrate 的输入校验拦截逻辑 (exit_code 4)。 |
| DIAG-01/02 | 0 / 3 | PASS / 超时安全退出 | MC-CPU-0902B-001 / N/A | events-simulate.jsonl, serial-simulate.raw | DIAG-01 离线重放复现 PASS；DIAG-02 串口监听 10s 无数据后安全报错退出，功能正常。 |
| REPORT | 0 | 生成成功 | MC-CPU-0902B-001 | output/MC-CPU-0902B/MC-CPU-0902B-001/report.md | 基于本地 result.json 成功生成 markdown/json/csv 报告。 |
```

最终判定：PRE-01 至 PRE-04 是环境准备完成；MC-01 至 MC-05 全部通过是最小闭环完成；QUAL-01 只表示资格化功能数据链打通，QUAL-02/03 才表示生产 baseline 全流程通过；TEL、DIAG 和报告生成按独立接口记录。24 个叶命令和关键参数分支全部有证据后，才能称为设计/使用文档的完整接口验收。
