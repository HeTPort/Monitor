# Vmin Judge 打包与设备验收测试文档

状态：适用于包含 2026-08-31 最小闭环修复的 `vmin_judge 2.0.1`、`avs-device-agent 0.1.1 protocol 1`、配置/事件/清单/基线/结果 schema v1

最后更新：2026-08-31。本文把 2026-08-31 NCH-004 现场记录暴露的平台身份、发布包资源解析、CPU policy、governor 和串口闭环问题纳入主流程；其中“已通过离线回归”与“仍需真机确认”严格分开。

对象：发布工程师、设备测试工程师、CPU/GPU workload 工程师、Monitor 维护者

目标：验证发布包能独立运行、真实设备接口可用、CPU/GPU 结果可信、故障分类正确，并收集足够证据支持后续代码调整。

## 1. 验收结论规则

测试结论分为四类：

| 结论 | 含义 |
|---|---|
| PASS | 用例步骤、退出码、结果文件、哈希和设备状态全部符合预期。 |
| DUT_FAIL（退出码 1） | 设备/工作负载的正确性、性能、遥测或关键内核事件失败；必须保留原生 workload 结果与退出码。 |
| INFRA_ERROR（退出码 3） | 传输、串口协议、设备代理、产物、哈希或状态恢复问题导致无法可靠判断 DUT。 |
| BLOCKED | 缺少设备、权限、基线、测试资产或测试环境，尚未真正执行；不得记为 PASS。 |

Monitor 进程退出码：

| 退出码 | 含义 | 典型场景 |
|---:|---|---|
| 0 | PASS | 完整事件流、workload PASS、策略通过、`agent_final` 和产物哈希完整。 |
| 1 | DUT_FAIL | checksum/golden、性能、温度、频率、关键内核事件等失败。 |
| 2 | SILENT_FAILURE | 在监控链路仍有效时 heartbeat/summary 消失。 |
| 3 | INFRA_ERROR | 串口断开、JSON/run ID/序号错误、输入帧 CRC（若提供）错误、代理异常、收集/恢复/哈希失败。 |
| 4 | INVALID_CONFIGURATION | 参数、配置、schema、路径、基线或 fingerprint 错误。 |
| 5 | UNSUPPORTED | 必需设备接口、工具或兼容基线缺失。 |
| 6 | USER_ABORT | 用户中止，仍应尝试清理和恢复。 |

原生 workload 的退出码与 Monitor 退出码不是同一字段。CPU/GPU workload 的非 PASS 结果必须保留在 `result.json` 的 `workload_result` 和 `workload_exit_code` 中，不能只保留 Monitor 的退出码。

## 2. 测试范围与优先级

| 优先级 | 测试域 | 必须验证的功能 |
|---|---|---|
| P0 | 发布包 | 独立 EXE、版本/schema、资源完整、非源码目录运行、只读安装目录、稳定退出码。 |
| P0 | 设备探测 | HDC/ADB、CPU/GPU/sysfs/debugfs/thermal、POSIX Shell、SHA-256、`dmesg`、`taskset`。 |
| P0 | 部署 | 推送、权限、远端 SHA-256、重复部署幂等、受控清理。 |
| P0 | 串口协议 | 单写者、JSONL、run ID、连续序号、可选输入 CRC、heartbeat、summary、`agent_final`；PC 落盘 CRC 可验证。 |
| P0 | CPU/GPU | golden、校准、基线审批、批量 PASS、原生失败结果保留。 |
| P0 | 状态安全 | PASS、FAIL、超时、中止和控制链路异常后的 governor/frequency/online 状态恢复。 |
| P1 | 故障注入 | checksum/golden mismatch、超时、遥测违规、关键内核事件、串口损坏。 |
| P1 | 产物 | 本地/设备 spool 收集、哈希校验、报告重建、离线回放。 |
| P1 | 长稳与带宽 | 平台配置波特率下无丢帧/乱序，原始 `dmesg` 不进入 UART。 |

### 2.1 最小闭环完成判据

本节是现场是否已经打通核心功能的唯一快速门槛。下面五项全部通过，才可声明“Monitor 最小闭环完成”：

| 门槛 | 必须通过的测试 | 通过条件 |
|---|---|---|
| MC-01 发布包自包含 | REL-03 `validate --all --package --offline` | 退出码 0；CPU/GPU workload、shader、agent 和配置均从 EXE/bundle 解析，不搜索 `D:\Tools`、用户目录或源码目录。 |
| MC-02 平台建档 | DEV-02 `probe --platform kirin9030 --full` | 退出码 0；身份为 Kirin9030；`required_missing=[]`；CPU policy/core 映射和 governor 支持值完整。每个平台+BSP+framework 组合通过一次即可归档。 |
| MC-03 串口配对 | DEV-03 `pair --verify` | 退出码 0；PC COM、设备 UART、baud 保存；不能只看到 marker 而没有 verification。 |
| MC-04 CPU/GPU live 闭环 | QUAL-00 两条 `smoke` | 两条命令均退出 0，且均有 `minimum_closed_loop=true`、`verdict=PASS`；事件包含 `agent_start`、workload `start/heartbeat/summary` 和 `agent_final`；PC `result.json` 判定完成。 |
| MC-05 测后取证 | 对任一 smoke 执行 RUN-03 `collect` 和 RUN-04 `report` | spool 哈希验证成功；`collection.json` 存在；`report.md/json/csv` 与 `result.json` verdict 一致。 |

以下项目不属于最小闭环的前置条件：10 次 golden、多板 calibration、baseline approve、30 次生产 PASS、故障注入和长稳。它们是后续资格认证/发布门槛，不能反过来替代 MC-01～MC-05。

`smoke` 复用生产事务中的目标域实时安全检查、hash 部署、agent/负载启动、COM-before-agent、UART 解码、PolicyEngine 和 ArtifactStore，但使用运行时临时基线且丢弃生成的 correctness reference，绝不会创建或批准生产 golden/baseline。smoke 的设备 spool 默认保留，便于测试结束后统一拉取。

“平台只做一次”指 full characterization 的人工审核和归档只做一次；每次 `smoke`/`golden`/`calibrate`/`run` 仍执行轻量实时 fail-closed 检查。设备身份、请求 governor、接口可读性或环境 readback 可能随 BSP/启动状态变化，不能从旧文件直接放行执行。

MC-01～MC-05 最短命令集如下。全局参数必须位于子命令之前；每条命令都应按 3.2 节立即保存 `$LASTEXITCODE`：

```powershell
# MC-01：确认发布 EXE 自包含；不得出现向 D:\Tools 或用户目录搜索 workload。
& $Exe --config-dir $ApprovedConfig --output-dir $Out --state-dir $State --json `
  validate --all --package --offline *> "$Evidence\MC-01-package.json"

# MC-02：Kirin9030 平台+BSP+framework 首次建档，后续只有平台/BSP/config fingerprint 变化才重做 full 审核。
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device `
  --output-dir $Out --state-dir $State --json `
  probe --platform $Platform --full *> "$Evidence\MC-02-probe.json"

# MC-03：建立并保存 UART 配对。
& $Exe --device $Device --state-dir $State pair --channel hdc --platform $Platform `
  --device-port $DeviceUart --pc-port $PcSerial --baudrate $Baudrate --timeout 5 --verify `
  *> "$Evidence\MC-03-pair.txt"

# MC-04：两条命令都必须 PASS。smoke 内部已经执行 hash 部署，不需要先单独 deploy。
$CpuSmokeId = "smoke-cpu-$Framework-$BoardId"
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --pc-serial $PcSerial `
  --baudrate $Baudrate --output-dir $Out --state-dir $State --json `
  smoke --profile cpu_smoke_kirin9030 --run-id $CpuSmokeId *> "$Evidence\MC-04-cpu-smoke.json"

$GpuSmokeId = "smoke-gpu-$Framework-$BoardId"
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --pc-serial $PcSerial `
  --baudrate $Baudrate --output-dir $Out --state-dir $State --json `
  smoke --profile gpu_smoke_kirin9030 --run-id $GpuSmokeId *> "$Evidence\MC-04-gpu-smoke.json"

# MC-05：验证一次设备 spool 拉取，再从 PC 的 result.json 重建报告。
& $Exe --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  collect --run-id $CpuSmokeId --verify-hashes --keep-remote *> "$Evidence\MC-05-collect.json"
& $Exe --output-dir $Out --state-dir $State --json `
  report --run-dir "$Out\$CpuSmokeId" --format markdown,json,csv *> "$Evidence\MC-05-report.json"
```

## 3. 测试环境与变量

### 3.1 必需环境

- Windows 测试 PC，能够访问 HDC 或 ADB，并有可用的 PC UART；端口号以设备管理器实际枚举为准。
- 至少一块单框架设备、一块双框架设备；生产基线校准至少覆盖两块代表性已知良品板。
- 设备镜像允许读取必需 sysfs，并允许设置 governor/frequency/online 状态。
- 设备代理使用固定 POSIX Shell 实现，不要求板端安装 Python；设备必须提供 `/bin/sh`，建议提供 `mkfifo`、`awk`、`sed` 和 `sha256sum`/`toybox sha256sum`。
- 已构建的 HarmonyOS CPU/GPU workload 和 Vulkan SPIR-V：
  - `tools/cpu-avs-workload`
  - `tools/gpu-avs-workload`
  - `tools/shaders/vulkan/fullscreen.vert.spv`
  - `tools/shaders/vulkan/workload.frag.spv`

### 3.2 PowerShell 测试变量

所有全局参数必须放在子命令之前。

```powershell
$Exe = 'D:\release\vmin_judge.exe'
$PackageId = 'vmin-judge-2.0.1-YYYYMMDD'
$Device = 'DEVICE_ID'
$BoardId = 'BOARD_001'
$Framework = 'single'       # single 或 dual
$Platform = 'kirin9030'     # 0831 NCH-004 的 /proc/cmdline 实测身份
$PcSerial = '<PC_UART_PORT>'
$DeviceUart = '<DEVICE_UART_PATH>' # 以板端/BSP 确认的业务 UART 为准，不要使用控制台
$Baudrate = 9600                 # 以所选平台配置和配对结果为准
$ApprovedConfig = 'D:\approved-config'
$RemoteRoot = '/data/local/tmp/avs'
$Out = "D:\MonitorTest\$PackageId\$Framework\$BoardId\output"
$State = "D:\MonitorTest\$PackageId\$Framework\$BoardId\state"
$Evidence = "D:\MonitorTest\$PackageId\$Framework\$BoardId\evidence"
New-Item -ItemType Directory -Force -Path $Out, $State, $Evidence | Out-Null
```

本文以 HDC 为例。ADB 环境将 `--transport hdc` 改为 `--transport adb`。有多个设备时必须传 `--device`，不得依赖自动选择。

每条命令执行后立即保存退出码，不能在运行其他命令后再读取 `$LASTEXITCODE`：

```powershell
# 示例：标准输出与错误输出合并留证；CMD 仍只看到本命令的最终对象/错误。
& $Exe --version *> "$Evidence\00-version.txt"
$VersionExit = $LASTEXITCODE
Set-Content -Encoding ascii "$Evidence\00-version.exit.txt" $VersionExit
```

若需要把 stdout/stderr 分开，使用 `1>` 和 `2>`；不得只保存截图。测试命令中的 `<...>`、`RUN_ID`、`DEVICE_ID` 都是占位符，执行前必须替换。

### 3.3 明日现场建议顺序（P0 快速路线）

按下面顺序执行；某一步未满足通过标准时先保存证据，再决定是否继续。不要跳过配置来源核对直接跑 golden。

| 顺序 | 用例 | 执行目标 | 继续条件 |
|---:|---|---|---|
| 1 | REL-03/REL-04 | 记录 EXE 版本、SHA-256，离线验证内置包和批准配置 | `2.0.1`；批准配置路径/hash 正确 |
| 2 | DEV-INFO | 一次性采集设备身份、UART、CPU、GPU、thermal、工具和权限 | 采集文件完整；不要求所有接口都存在 |
| 3 | DEV-02 | 用同一个 `--config-dir` 做 Kirin9030 full probe | 身份匹配；policy/core、governor、v500 hang 路径和值明确 |
| 4 | DEV-03 | 显式 UART/COM/baud 配对 | `SUCCESS` 且 verify 成功 |
| 5 | DEV-04 | 两次部署并检查 agent 版本 | 第一次 pushed、第二次 unchanged、agent 0.1.1 |
| 6 | QUAL-00 | CPU/GPU 各 1 次 baseline-free live smoke | 两次均 `minimum_closed_loop=true` 且有完整 `result.json` |
| 7 | QUAL-01/02 | 预热后做正式 CPU/GPU golden | 每次 PASS，CPU checksum/GPU readback 一致 |
| 8 | RUN-01 | 有批准 baseline 后执行 normal run | UART 有完整事件，CMD 仅最终结果/错误 |
| 9 | RUN-03/第 13 节 | 收集失败 spool，打包现场证据 | hash 验证成功，反馈包可离线分析 |

若尚无批准 baseline，仍可完成 MC-01～MC-05；不应把 `run --baseline auto` 的配置失败记成最小闭环失败。CPU 和 GPU 分开推进：CPU smoke 不应被 GPU-only `gpu.hang_count` 阻断。

## 4. 打包前和发布包测试

### REL-01：源码离线回归

```powershell
python -m unittest discover -s tests -v *> "$Evidence\REL-01-unittest.txt"
$LASTEXITCODE
```

通过标准：全部测试通过，退出码为 0。当前仓库离线测试已包含任意端口/波特率、marker 重发、分片接收、配对故障分类、HDC 远端退出码和 Shell agent 混合温标回归；发布前仍需在实际构建环境重跑。

覆盖内容包括路径、schema、协议碎片/粘包/CRC/乱序、策略优先级、基线不可变性、校准、设备代理单写者与恢复、部署幂等、CLI、产物哈希和离线回放。

### REL-02：构建发布包

```powershell
python -m pip install -r requirements-dev.txt
.\scripts\build.ps1 *> "$Evidence\REL-02-build.txt"
$BuildExit = $LASTEXITCODE
Get-FileHash .\dist\vmin_judge.exe -Algorithm SHA256 |
  Format-List *> "$Evidence\REL-02-package-sha256.txt"
```

通过标准：

- `$BuildExit` 为 0；构建脚本没有使用 `-SkipTests`。
- 缺少 workload、shader、pyserial、PyYAML 或 PyInstaller 时必须阻断构建，不能生成“看似可用”的不完整包。
- `dist/vmin_judge.exe --version` 由构建脚本自动冒烟成功。
- 保存 EXE 大小、SHA-256、构建机 OS/Python/PyInstaller 版本和源码 commit ID。

### REL-03：独立包与 schema

将 EXE 单独复制到一台没有源码树的干净 PC/目录中执行：

```powershell
Push-Location $env:TEMP
& $Exe --version *> "$Evidence\REL-03-version.txt"
$VersionExit = $LASTEXITCODE
& $Exe --help *> "$Evidence\REL-03-help.txt"
$HelpExit = $LASTEXITCODE
& $Exe --output-dir $Out --state-dir $State --json validate --all --package --offline `
  *> "$Evidence\REL-03-validate.json"
$ValidateExit = $LASTEXITCODE
& $Exe --output-dir $Out --state-dir $State --json list-profiles `
  *> "$Evidence\REL-03-profiles.json"
$ProfilesExit = $LASTEXITCODE
Pop-Location
```

通过标准：

- 四个退出码都为 0。
- 版本为 `vmin_judge 2.0.1`，并报告 `config=1 event=1 manifest=1 baseline=1 result=1`。
- `validate` 的 `valid=true`、`errors=[]`，并检查到两个 profile、平台、workload 配置/二进制、GPU shader、设备代理和 kernel rule。
- `list-profiles` 至少包含 `cpu_mixed_big4` 和 `gpu_vulkan_mixed`。
- 当前工作目录保持不变；所有可写内容仅进入 `$Out` 或 `$State`。
- 若出现 `PyYAML is required` 或 `pyserial is required`，发布包直接判定不合格。

### REL-04：只读安装目录和外部配置

把 EXE 所在目录设为普通测试账号只读，从另一个目录运行 REL-03，并分别测试：

```powershell
& $Exe --config-dir D:\approved-config `
  --output-dir $Out --state-dir $State --json validate --all --package --offline
```

通过标准：EXE/临时 bundle 目录无写入；输入按“绝对路径、声明文件 owner 相对路径、`--config-dir`（兼容带/不带 `config` 的两种根）、当前目录、EXE 外部资源、内置资源”的顺序解析；相对输出只写入 `$Out`。对本次批准配置，`--config-dir` 必须先于当前目录/内置同名平台文件生效。

### REL-05：错误边界

```powershell
& $Exe --output-dir $Out --state-dir $State --json simulate --events does-not-exist.jsonl `
  *> "$Evidence\REL-05-invalid-config.json"
$LASTEXITCODE
```

通过标准：退出码为 4，输出为稳定错误对象，无 Python traceback。对缺少资源、错误 schema、无效 baseline bundle 各执行一次同类检查。

## 5. 设备接口和命令清单

| 命令/接口 | 测试目的 | 主要输出 |
|---|---|---|
| `probe` | 探测平台、权限、工具、CPU/GPU/thermal 接口 | `$Out/probes/<serial>/capabilities.json` |
| `pair` | 建立设备 UART 与 PC COM 口对应关系 | `$State` 下的 pairing 状态及控制台结果 |
| `deploy` | 推送 agent/workload/config/shader/golden 并校验哈希 | `$Out/deployment-manifest.json` |
| `golden cpu/gpu` | 已知良品的正确性基准 | `$Out/qualification/<qualification-id>/...` |
| `calibrate cpu/gpu` | 统计样本并生成阈值和 draft baseline | `$Out/qualification/<baseline-id>/proposed-baseline.json` |
| `baseline` | 审核、批准、导入/导出、弃用不可变基线 | `$State/baselines/<id>/...` |
| `run` | 使用批准基线执行、监控、判定和产物落盘 | `$Out/<run-id>/result.json` 等 |
| `monitor` | UART/协议诊断，不作 DUT PASS 判定 | `$Out/monitor-*/...` |
| `collect` | 拉取设备 spool 并验证哈希 | `$Out/<run-id>/device-spool`、`collection.json` |
| `report` | 从已有 `result.json` 重建报告 | `report.md/json/csv` |
| `simulate` | 从 `events.jsonl` 或 `serial.raw` 离线重放 | 新的回放 run 目录和 `result.json` |

设备代理由 PC 端根据已解析 manifest 以安全参数启动；设备端无需解析 JSON。直接健康检查接口为：

```text
avs-device-agent --version
```

事件接口为每行一个 UTF-8 JSON，关键字段是 `schema_version`、`run_id`、`seq`、`timestamp_ms`、`source`、`type`、`payload`；Shell backend 不发送 CRC，但 PC 仍严格校验 run ID 和连续序号。正常顺序至少应能看到 `agent_start`、`start`、多个 `heartbeat`/`telemetry`、`summary` 和最后的 `agent_final`。

## 6. 设备基础与接口探测

若使用外部批准配置，先固定一个根目录，并在后续所有 `validate`、`probe`、`golden`、`calibrate`、`run` 命令的子命令之前传入同一个全局参数：

```powershell
$ApprovedConfig = 'D:\approved-config'
& $Exe --config-dir $ApprovedConfig --output-dir $Out --state-dir $State --json `
  validate --profile gpu_vulkan_mixed --offline `
  *> "$Evidence\DEV-00-approved-config.json"
```

目录既可以是 `$ApprovedConfig\config\platforms\kirin9020.yaml`，也可以让根目录直接从 `$ApprovedConfig\platforms\kirin9020.yaml` 开始。通过标准不是只有 `valid=true`：必须核对 `resolved_configs` 中的实际绝对路径和 SHA-256 指向修改后的文件。

先记录文件自身 hash，再与 `validate` 输出交叉确认：

```powershell
$PlatformConfig = @(
  "$ApprovedConfig\config\platforms\kirin9020.yaml",
  "$ApprovedConfig\platforms\kirin9020.yaml"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $PlatformConfig) { throw 'approved kirin9020.yaml not found' }
Get-Item $PlatformConfig | Format-List FullName,Length,LastWriteTime `
  *> "$Evidence\DEV-00-platform-file.txt"
Get-FileHash $PlatformConfig -Algorithm SHA256 | Format-List `
  *> "$Evidence\DEV-00-platform-sha256.txt"
Select-String -Path $PlatformConfig -Pattern 'hvgr_kmd_v500','gpu_hang_count' `
  *> "$Evidence\DEV-00-platform-v500-lines.txt"
```

预期结果：三个文件都非空；`validate` 的 platform `path` 与 `$PlatformConfig` 完全一致，`sha256` 与 `Get-FileHash` 一致。这里验证的是 PC 端配置选择；后续 `deploy` 不会把平台 YAML 推到设备，不能用部署清单证明配置覆盖成功。

### DEV-INFO：设备深度信息采集（单独执行，一块板一次）

这一组命令只读，目的是在 probe/golden 异常时保留原始设备事实。允许某些文件不存在，但每条命令的输出和错误都必须保留。不要因为 probe 已生成 `capabilities.json` 就省略原始采集。

1. 身份、镜像、内核和存储：

```powershell
hdc -t $Device shell getprop *> "$Evidence\DEV-INFO-01-getprop.txt"
hdc -t $Device shell uname -a *> "$Evidence\DEV-INFO-01-uname.txt"
hdc -t $Device shell cat /proc/cmdline *> "$Evidence\DEV-INFO-01-cmdline.txt"
hdc -t $Device shell cat /proc/cpuinfo *> "$Evidence\DEV-INFO-01-cpuinfo.txt"
hdc -t $Device shell id *> "$Evidence\DEV-INFO-01-id.txt"
hdc -t $Device shell mount *> "$Evidence\DEV-INFO-01-mount.txt"
hdc -t $Device shell df -h /data/local/tmp *> "$Evidence\DEV-INFO-01-storage.txt"
```

2. UART 与控制台占用。这里仅做读操作，不修改设备端波特率：

```powershell
hdc -t $Device shell ls -l $DeviceUart *> "$Evidence\DEV-INFO-02-uart-path.txt"
hdc -t $Device shell cat /proc/consoles *> "$Evidence\DEV-INFO-02-consoles.txt"
hdc -t $Device shell sh -c 'ls -l /dev/tty* 2>&1' `
  *> "$Evidence\DEV-INFO-02-all-tty.txt"
hdc -t $Device shell sh -c "stty -a < '$DeviceUart' 2>&1 || toybox stty -a < '$DeviceUart' 2>&1" `
  *> "$Evidence\DEV-INFO-02-stty.txt"
hdc -t $Device shell cat /proc/tty/driver/serial `
  *> "$Evidence\DEV-INFO-02-serial-driver.txt"
```

预期至少能确认 `$DeviceUart` 存在、权限和 owner/group；若它出现在 `/proc/consoles`，必须先由 BSP 负责人确认可否作为业务 UART。`stty` 不可用不是 Monitor 失败，但必须记录 BSP 侧真实 baud/data/parity/stop 配置。

3. CPU topology 和每个 cpufreq policy 的真实值：

```powershell
hdc -t $Device shell cat /sys/devices/system/cpu/present `
  *> "$Evidence\DEV-INFO-03-cpu-present.txt"
hdc -t $Device shell cat /sys/devices/system/cpu/possible `
  *> "$Evidence\DEV-INFO-03-cpu-possible.txt"
hdc -t $Device shell sh -c 'for p in /sys/devices/system/cpu/cpufreq/policy*; do
  [ -d "$p" ] || continue
  echo "=== $p ==="
  ls -ld "$p"
  for f in affected_cpus related_cpus cpuinfo_min_freq cpuinfo_max_freq scaling_min_freq scaling_max_freq scaling_cur_freq scaling_governor scaling_available_governors scaling_available_frequencies; do
    printf "%s=" "$f"; cat "$p/$f" 2>&1
  done
done' *> "$Evidence\DEV-INFO-03-cpufreq-policies.txt"
```

预期结果：每个 `policyN` 单独记录。重点确认 heterogeneous policy 的 `cpuinfo_max_freq`，不能把一个 cluster 的 2508000 套到所有 policy。把 policy、related CPU、max frequency 三列抄入 `reproduction-notes.md`。

4. GPU 驱动、devfreq 和 hang/reset/fault 计数器：

```powershell
hdc -t $Device shell sh -c 'for p in
  /sys/module/hvgr_kmd_v500/parameters/gpu_hang_count
  /sys/module/hvgr_kmd_v350/parameters/gpu_hang_count; do
  echo "=== $p ==="; ls -l "$p" 2>&1; cat "$p" 2>&1
done' *> "$Evidence\DEV-INFO-04-gpu-hang.txt"
hdc -t $Device shell sh -c 'find /sys/class/devfreq /sys/devices -maxdepth 6 -type f \( -name cur_freq -o -name min_freq -o -name max_freq -o -name governor -o -name available_governors -o -name load -o -name utilization -o -name power_policy \) 2>&1' `
  *> "$Evidence\DEV-INFO-04-gpu-candidates.txt"
hdc -t $Device shell sh -c 'ls -ld /sys/module/hvgr_kmd_v500 /sys/module/hvgr_kmd_v350 2>&1; dmesg 2>&1 | tail -n 300' `
  *> "$Evidence\DEV-INFO-04-gpu-driver-dmesg-tail.txt"
```

预期结果：v500 设备的首个 hang 候选存在、可读、空闲值通常为数值 0；若不存在或不可读，记录 `ls`/`cat` 原始错误、驱动模块名、BSP/内核 build，不要直接把该 capability 改成 optional。`find` 不可用时用 `ls -lR /sys/class/devfreq` 替代并注明。

5. Thermal 原始温标和工具能力：

```powershell
hdc -t $Device shell sh -c 'for z in /sys/class/thermal/thermal_zone*; do
  [ -d "$z" ] || continue
  printf "%s type=" "$z"; cat "$z/type" 2>&1
  printf "%s temp=" "$z"; cat "$z/temp" 2>&1
done' *> "$Evidence\DEV-INFO-05-thermal-zones.txt"
hdc -t $Device shell sh -c 'for c in sh awk sed mkfifo sha256sum taskset dmesg timeout; do
  printf "%s: " "$c"; command -v "$c" 2>&1 || true
done
toybox --help 2>&1 | head -n 20' *> "$Evidence\DEV-INFO-05-tools.txt"
hdc -t $Device shell sh -c 'ps -ef 2>&1 | grep -E "avs-device-agent|avs-workload" | grep -v grep || true; ls -l /data/local/tmp/avs/runs 2>&1' `
  *> "$Evidence\DEV-INFO-05-processes-runs.txt"
```

预期结果：每个 thermal zone 同时保留 `type` 与未经换算的 `temp`；工具缺失由后续 profile scope 判断，不能仅凭 `command -v` 的某一行给整个平台判 PASS。记录采集时间、设备时区和 PC 时区，便于对齐 UART/`dmesg` 时间。

### DEV-01：HDC/ADB 和板端工具

先用原生命令确认控制链路，仅作辅助证据：

```powershell
hdc list targets
hdc -t $Device shell sh -c 'echo shell-ok'
hdc -t $Device shell mkfifo --help
hdc -t $Device shell sha256sum --help
hdc -t $Device shell dmesg --help
hdc -t $Device shell taskset --help
```

如果设备只有 `toybox sha256sum`，Monitor 会自动回退。原生命令的 stdout、stderr 和退出码都要保存。

### DEV-02：完整 probe

在单框架和双框架镜像各执行一次：

`--platform` 必须显式提供；程序不再把未知硬件默认为 Kirin9020。

```powershell
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  probe --platform $Platform --full `
  *> "$Evidence\DEV-02-probe.json"
$ProbeExit = $LASTEXITCODE
Set-Content -Encoding ascii "$Evidence\DEV-02-probe.exit.txt" $ProbeExit
Copy-Item "$Out\probes\$Device\capabilities.json" `
  "$Evidence\DEV-02-capabilities-$Framework.json"
```

通过标准：`$ProbeExit=0`、`supported=true`、`required_missing=[]`。重点核对：

- 身份：`platform_identity.matched=true`，hardware/chiptype 的 `actual` 均为 `Kirin9030`。用 `kirin9020` 适配器探测本设备必须 fail-closed，不能继续部署。
- CPU policy：`cpu_topology.policies` 能读出每个 policy 的 `affected_cpus`/`related_cpus`，`policy_by_cpu` 覆盖实际 CPU；不再执行易受 PowerShell/HDC 字符串展开影响的手写 `for p in ...` 命令。
- governor：CPU/GPU governor 记录包含 `available_value_paths`、`supported_values_by_path`；后续配置请求的值必须在每个目标路径上可验证。

- CPU：每核 frequency/min/max/governor/online、`/proc/stat` utilization、按 thermal `type` 匹配的温度。
- GPU：frequency/min/max/governor/utilization/power policy/hang count、按 thermal `type` 匹配的温度。
- Kirin9030 当前优先探测 `/sys/module/hvgr_kmd_v500/parameters/gpu_hang_count`，并保留 v350 作为旧 BSP 回退；`candidate_paths` 和实际 `paths` 必须与设备接口一致。
- 工具：`device.shell`、`device.sha256sum` 可用；CPU profile 运行时 `device.taskset` 必需；kernel monitor 开启时 `device.dmesg` 必需。
- 每个 metric 都有实际路径、单位、值和 provenance，不允许只有“可用”布尔值。温度还要核对 `raw_value`、配置/实际采用单位、归一化摄氏值、`valid` 和 `invalid_reason`。
- 单/双框架可以使用不同真实路径，但语义、单位和必需能力必须等价。把两个 `capabilities.json` 做字段级 diff，并注明所有差异。
- 顶层 `platform_config.path`/`sha256` 与 DEV-00 一致，并且 `external_override=true`。如果仍指向 EXE bundle，立即停止 golden 并提交 DEV-00/DEV-02 证据。

`--full` 是平台全量清单。实际 CPU/GPU `run`、`golden`、`calibrate` 只探测所选 profile 的目标域并按 required capability 判定：CPU profile 不再扫描或受 GPU-only `gpu.hang_count` 影响；GPU profile 仍必须探测并在缺失时阻断。

当前实现每次都从设备实时读取；`--refresh` 作为兼容参数保留。DEV-02 的完整输出按平台+BSP+framework 归档一次；运行命令里的目标域实时检查是安全门，不是要求人工重复做 DEV-INFO/DEV-02 验收。BSP、平台配置 fingerprint 或硬件身份变化后必须重新执行并审核 DEV-02。

缺少必需接口时预期退出码为 5。不要通过删除 profile 的 required 项来“通过”验收。

### DEV-03：串口配对和诊断监听

配对时只连接一块目标设备，避免旧 `pair` 接口选择歧义：

```powershell
& $Exe --device $Device --state-dir $State pair --channel hdc --platform $Platform `
  --device-port $DeviceUart --pc-port $PcSerial --baudrate $Baudrate --timeout 5 --verify `
  *> "$Evidence\DEV-03-pair.txt"
$PairExit = $LASTEXITCODE
Set-Content -Encoding ascii "$Evidence\DEV-03-pair.exit.txt" $PairExit
Copy-Item "$State\pair-diagnostic.json" "$Evidence\DEV-03-pair-diagnostic.json"
Get-Content "$State\pairing.conf" *> "$Evidence\DEV-03-pairing-conf.json"
```

不传显式端口时，PC 端来自实际枚举，设备端来自实际扫描；只有传入 `--platform $Platform` 时才把该平台的 `serial.uart_candidates` 用作扫描失败后的候选。通用配对层不猜测 `/dev/tty*` 或 `COM*`。算法先打开 PC 端、等待短暂稳定、清空旧输入，在一个总超时内最多发送 3 次唯一 marker，并持续匹配可分片到达的字节。

配对成功后，先完成 DEV-04 部署并启动 device agent/workload（或另一个明确的 JSONL 帧生产者），随后才能用 `monitor` 验证协议流：

```powershell
& $Exe --pc-serial $PcSerial --baudrate $Baudrate --output-dir $Out --state-dir $State `
  monitor --save-raw --timeout 60
```

通过标准：pair 退出码 0，`pair-diagnostic.json` 的最终记录为 `SUCCESS` 且 verification 成功，`pairing.conf` 保存本次 device port/PC port/baud；在帧生产者运行时，monitor 能识别连续事件。原始 pair marker 不是事件帧，不能用它单独判定 monitor。`monitor` 是诊断命令，结果必须是 `NOT_EVALUATED`，不能输出 DUT PASS。

Pair 只在 PC 端打开串口时设置 `$Baudrate`。设备 UART 应由 BSP 预先配置，程序不会自动执行 `stty` 修改设备端波特率，避免破坏控制台。失败码至少区分 `NO_DEVICE_PORTS`、`NO_PC_PORTS`、`PC_PORT_BUSY`、`PC_PORT_OPEN_FAILED`、`DEVICE_ECHO_FAILED`、`NO_RX_BYTES` 和 `MARKER_NOT_FOUND`。若失败，采集 `stty -a < $DeviceUart`（只读诊断）、BSP 配置及 `pair-diagnostic.json`；正常 marker 和串口正文不打印到控制台，诊断文件只保留有界十六进制预览。

### DEV-04：部署与幂等

```powershell
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  deploy --target all --verify-hashes *> "$Evidence\DEV-04-deploy-first.json"
$DeployFirstExit = $LASTEXITCODE
Copy-Item "$Out\deployment-manifest.json" "$Evidence\DEV-04-manifest-first.json"

& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  deploy --target all --verify-hashes *> "$Evidence\DEV-04-deploy-second.json"
$DeploySecondExit = $LASTEXITCODE
Copy-Item "$Out\deployment-manifest.json" "$Evidence\DEV-04-manifest-second.json"

hdc -t $Device shell /data/local/tmp/avs/bin/avs-device-agent --version `
  *> "$Evidence\DEV-04-agent-version.txt"
```

通过标准：两个部署退出码为 0；首次在空设备根上所有项应为 `pushed`，若设备已有相同 hash 则允许 `unchanged`，但本次发生变化的 agent/资产必须 `pushed`，且所有远端 hash 与本地一致；第二次所有项为 `unchanged`，不重复传输；agent 报告 `0.1.1 protocol 1`。`--clean-stale` 只允许删除先前 manifest 记录且位于 `/data/local/tmp/avs` 内的文件，需要单独做一次安全边界测试。

部署清单应包含 agent、所选 workload/config/shader 等设备执行资产，但不包含 PC 侧 `config/platforms/kirin9020.yaml`。这是预期行为：平台 YAML 由 PC 在 `validate`/`probe`/manifest 构建阶段读取，设备 agent 只接收已解析的安全 argv；“平台 YAML 未部署”不能作为覆盖不生效的证据。

## 7. CPU/GPU 资格认证与基线

当前 profile 中 `baseline: null`，所以不能直接执行生产 `run --baseline auto`。必须先生成 golden、完成多板校准并批准 baseline。

### QUAL-00：baseline-free 最小 live 闭环

正式 golden 前，CPU/GPU 各执行一次独立 `smoke`。它使用短 workload 配置，不设置未经0831证据确认的 affinity/governor/frequency，不创建 qualification manifest 或生产 baseline：

```powershell
$CpuSmokeId = "smoke-cpu-$Framework-$BoardId"
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device `
  --pc-serial $PcSerial --baudrate $Baudrate --output-dir $Out --state-dir $State --json `
  smoke --profile cpu_smoke_kirin9030 --run-id $CpuSmokeId `
  *> "$Evidence\QUAL-00-cpu-smoke.json"
$CpuSmokeExit = $LASTEXITCODE
Set-Content -Encoding ascii "$Evidence\QUAL-00-cpu-smoke.exit.txt" $CpuSmokeExit

$GpuSmokeId = "smoke-gpu-$Framework-$BoardId"
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device `
  --pc-serial $PcSerial --baudrate $Baudrate --output-dir $Out --state-dir $State --json `
  smoke --profile gpu_smoke_kirin9030 --run-id $GpuSmokeId `
  *> "$Evidence\QUAL-00-gpu-smoke.json"
$GpuSmokeExit = $LASTEXITCODE
Set-Content -Encoding ascii "$Evidence\QUAL-00-gpu-smoke.exit.txt" $GpuSmokeExit
```

每条命令后的检查方法：

```powershell
$LatestCpuRun = Get-Item "$Out\$CpuSmokeId"
$LatestGpuRun = Get-Item "$Out\$GpuSmokeId"

foreach ($RunDir in @($LatestCpuRun, $LatestGpuRun) | Where-Object { $_ }) {
  "=== $($RunDir.FullName) ==="
  Get-Content "$($RunDir.FullName)\result.json" | ConvertFrom-Json |
    Select-Object run_id,verdict,exit_code,event_count,workload_result,workload_exit_code,liveness
  Select-String -Path "$($RunDir.FullName)\events.jsonl" `
    -Pattern '"type":"environment"','"type":"error"','"type":"summary"','"type":"agent_final"'
}
```

一次 live 事务的代码行为和预期顺序如下：

1. PC 先解析 smoke profile/Kirin9030 platform，并做身份和目标域实时安全检查。CPU smoke 只要求 CPU 域，GPU smoke 才要求 `gpu.hang_count`。
2. 资产做 SHA-256 部署/校验；平台 YAML 不部署。
3. PC 先打开并清空 COM 输入缓冲，再通过 HDC 启动 agent，防止丢掉最早的 `agent_start`。
4. agent 把环境 apply/readback 结果、workload 原生事件、5 秒遥测、过滤后的 kernel 事件、summary、restore 和 `agent_final` 保留在 UART/设备 spool。
5. PC 的统一判断引擎消费 UART：workload heartbeat/summary 决定 workload liveness，telemetry 不能掩盖 workload 静默；最终生成 `result.json`。
6. CMD 默认不复制 heartbeat、telemetry 或正常环境事件，只输出最终 JSON 对象；失败时额外给出精简错误。完整证据在 run 目录。

最小闭环通过标准：`$CpuSmokeExit=0` 且 `$GpuSmokeExit=0`；两个命令输出都包含 `minimum_closed_loop=true` 和唯一 PASS run；每个 run 都有可读 `result.json`、`serial.raw`、`events.jsonl`、`artifact-hashes.json`、`workload-summary.json`；事件顺序中存在 `agent_start`、workload `start`/`heartbeat`/`summary`、环境恢复和 `agent_final`，且无 run ID/seq/JSON 错误。任何 DUT_FAIL/INFRA_ERROR/SILENT_FAILURE 都表示最小闭环尚未通过，不能仅以“有产物”关闭。

smoke 的 correctness reference 标记为 `discard`，不得交给 `baseline approve`。smoke PASS 后设备 spool 保留；立即用 RUN-03 的 `collect --verify-hashes --keep-remote` 验证拉取，再用 RUN-04 生成报告。

CPU 环境 readback 必须按 probe 返回的每个 `policyN` 使用自己的 platform max。当前已观察的 Kirin9020 示例为 1550000、2050000、2094000、2508000 kHz 四种 policy max；以 DEV-INFO 当天实测为准。若所有 policy 都被请求为 2508000，说明旧的路径映射问题仍存在，提交所有 `environment` 事件和 cpufreq policy 清单。

### QUAL-01：CPU golden

```powershell
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --pc-serial $PcSerial `
  --baudrate $Baudrate --output-dir $Out --state-dir $State --json `
  golden cpu --profile cpu_mixed_big4 --runs 10 --known-good --board-id $BoardId `
  *> "$Evidence\QUAL-01-cpu-golden.json"
$CpuGoldenExit = $LASTEXITCODE
Set-Content -Encoding ascii "$Evidence\QUAL-01-cpu-golden.exit.txt" $CpuGoldenExit
```

通过标准：10 次均为 PASS；每次恰有一个 `golden` 事件；checksum 完全一致；环境为 cores 4–7 online、performance governor、platform max frequency、35–60 °C；golden manifest 包含 workload/config/profile hash 和板号。正式审批前应使用已独立确认的 `--accept-checksum` 再做一次绑定检查。

第二批现场记录中的真实温度约为 30–31.4 °C，低于当前 profile 的 35 °C 下限。完成环境/串口修复后，应先把板卡稳定预热到 35–60 °C 再执行正式 golden；不要通过放宽阈值来掩盖冷机条件。

live golden 尚未全部通过时，每次运行目录直接位于 `$Out\golden-cpu-*`；只有全部 live run 通过并完成聚合后才创建 `$Out\qualification\<qualification-id>`。失败后不要把缺少 `qualification` 目录误判为输出丢失。

### QUAL-02：GPU golden

```powershell
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --pc-serial $PcSerial `
  --baudrate $Baudrate --output-dir $Out --state-dir $State --json `
  golden gpu --profile gpu_vulkan_mixed --runs 10 --known-good --board-id $BoardId `
  --readback-name gpu-golden.rgba *> "$Evidence\QUAL-02-gpu-golden.json"
$GpuGoldenExit = $LASTEXITCODE
Set-Content -Encoding ascii "$Evidence\QUAL-02-gpu-golden.exit.txt" $GpuGoldenExit
```

通过标准：运行前 DEV-02 中 `gpu.hang_count` 已解析到 v500（或有证据的兼容路径），值是可解析计数器；10 次 raw readback 字节数和 SHA-256 完全一致；shader/workload/config hash、驱动/BSP/板号均被记录；`verify_mode` 不是 `none`；原始 `.rgba` 按二进制缓冲区保存，不当作图片解码。

若 GPU smoke/golden 在 capability 阶段退出 5，核对错误中缺失的是哪个 metric，并同时提交 `DEV-00-platform-*`、`DEV-02-probe.json`、`DEV-INFO-04-gpu-hang.txt`。若 probe 已看到正确 v500 路径但 live 仍缺失，属于 profile scope/manifest 构建缺陷；若 probe 顶层配置 path/hash 错误，属于批准配置选择问题。

### QUAL-03：多板校准

默认策略要求至少 2 块板、至少 20 个合格样本，建议每块板采集 30 次。最终校准使用多次 `--run-dir BOARD_ID=PATH`。下面从两个样本根目录自动生成完整参数，避免示例只列少数目录后又意外触发 live 补跑：

```powershell
$RunDirArgs = @()
Get-ChildItem D:\samples\cpu\B1 -Directory | ForEach-Object {
  $RunDirArgs += @('--run-dir', "BOARD_001=$($_.FullName)")
}
Get-ChildItem D:\samples\cpu\B2 -Directory | ForEach-Object {
  $RunDirArgs += @('--run-dir', "BOARD_002=$($_.FullName)")
}
$RunCount = $RunDirArgs.Count / 2

& $Exe --config-dir $ApprovedConfig --output-dir $Out --state-dir $State --json calibrate cpu `
  --profile cpu_mixed_big4 --runs $RunCount --board-id COHORT `
  --temperature-range 35:60 --golden D:\golden\cpu-golden.json `
  --baseline-id kirin9020-cpu-mixed-big4-v1 @RunDirArgs
```

GPU 使用同样方式改成 `calibrate gpu`、GPU profile 和 GPU golden。

当前接口限制：一次 live `calibrate` 只连接一块设备，但默认策略要求两块板；仓库没有独立的 `qualification collect` 命令。现场应保留每块板 live calibration 产生的 run 目录，再在最终命令中合并。若单板采样完成后仅因 cohort 不足返回非零，必须将完整命令、已生成 run 目录和错误一起反馈，不能删除默认多板约束。建议后续代码新增“只采样、不创建 baseline”的命令。

通过标准：至少两个不同 `board_id`；`accepted_count >= 20`；温度越界、遥测缺失、throttle、环境不匹配样本列入 rejected 而不是静默丢弃；输出分布、margin、fingerprint、golden hash 和 draft baseline。

### QUAL-04：基线审核、批准、导入导出

```powershell
& $Exe --state-dir $State --json baseline show kirin9020-cpu-mixed-big4-v1
& $Exe --state-dir $State --json baseline approve kirin9020-cpu-mixed-big4-v1 --approver TEST_LEAD
& $Exe --output-dir $Out --state-dir $State --json baseline export `
  kirin9020-cpu-mixed-big4-v1 --output "$Evidence\cpu-baseline.zip"
& $Exe --state-dir $State --json baseline list --status approved
```

在一个全新的 `$State` 中执行 `baseline import` 并再次 `show`。通过标准：bundle hash 正确，批准后的 `baseline.json` 不可变；篡改一个字节后，validate/run/export 必须拒绝并返回 4，不能继续跑 DUT。

## 8. 生产运行、收集与报告

### RUN-01：CPU/GPU 基本 PASS

```powershell
& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --pc-serial $PcSerial `
  --baudrate $Baudrate --output-dir $Out --state-dir $State --json run `
  --profile cpu_mixed_big4 --baseline kirin9020-cpu-mixed-big4-v1 `
  --repeat 3 --run-id "CPU-$Framework-$BoardId" --kernel-monitor critical `
  *> "$Evidence\RUN-01-cpu.json"
$CpuExit = $LASTEXITCODE

& $Exe --config-dir $ApprovedConfig --transport hdc --device $Device --pc-serial $PcSerial `
  --baudrate $Baudrate --output-dir $Out --state-dir $State --json run `
  --profile gpu_vulkan_mixed --baseline kirin9020-gpu-vulkan-mixed-v1 `
  --repeat 3 --run-id "GPU-$Framework-$BoardId" --kernel-monitor critical `
  *> "$Evidence\RUN-01-gpu.json"
$GpuExit = $LASTEXITCODE
```

通过标准：两个退出码均为 0；每次 run 的 verdict 为 PASS；无 DUT/infra reasons；summary 和 `agent_final` 都存在；required telemetry 每类至少有一个实例；环境 readback 匹配；退出后状态恢复。`--repeat 3 --run-id NAME` 应生成 `NAME-001`、`NAME-002`、`NAME-003` 三个独立目录，任何一次旧 run ID/seq 混入都应为 INFRA_ERROR。

正常 CMD 验收：`RUN-01-*.json` 是一个最终命令对象，包含 `repeat`、总 `exit_code`、每次 run 的 `run_id/verdict/exit_code/result`；正常时不应逐条打印 heartbeat、telemetry、environment 或 kernel 正文。失败的单次 run 还应出现 `errors`，环境问题至少带 `phase/path/requested/actual`，完整 reasons 仍以该 run 的 `result.json` 为准。

设备 spool 规则：正常 `run` 的 PASS 默认只删除精确的 `$RemoteRoot/runs/<run-id>/spool`；DUT_FAIL/INFRA_ERROR 默认保留。要在 PASS 场景专门验证收集流程，可先做一次带 `--keep-device-spool` 的 `run --repeat 1`，收集后再删除。`golden`/`calibrate` 不接受这个开关，其 live 失败证据按各自 run 目录和设备 spool 保留情况收集。

### RUN-02：无部署运行与 fingerprint

在 DEV-04 后执行同一 baseline 的 `run --no-deploy`，预期成功且只验证已有远端 hash。修改任一 workload/config/shader 字节后再次执行，预期在运行前因 fingerprint 或 asset hash 不匹配退出 4/3，不能复用旧 golden。

### RUN-03：设备 spool 收集

第一次保留远端证据：

```powershell
& $Exe --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  collect --run-id RUN_ID --verify-hashes --keep-remote `
  *> "$Evidence\RUN-03-collect-keep.json"
```

确认本地 hash 后，可再次执行不带 `--keep-remote` 的收集；只有 hash 验证成功后才允许删除该具体 run 的远端 spool。通过标准：`collection.json` 记录来源、目标、字节数、verified 和 remote_removed；路径必须限定在 `/data/local/tmp/avs/runs/<run-id>` 下。

设备端 `artifact-hashes.json` 的结构应为 `{"schema_version":1,"sha256":{...}}`。若 hash 缺失/不匹配，`collect` 必须退出 3 且保留远端；不得手工删除后重跑来掩盖收集错误。

### RUN-04：报告与离线回放

```powershell
& $Exe --output-dir $Out --state-dir $State --json report `
  --run-dir "$Out\RUN_ID" --format markdown,json,csv

& $Exe --output-dir "$Out\replay" --state-dir $State --json simulate `
  --events "$Out\RUN_ID\events.jsonl" --profile cpu_mixed_big4 `
  --baseline kirin9020-cpu-mixed-big4-v1
```

通过标准：报告与 `result.json` 的 verdict/退出码/workload 结果一致；原始 run 文件未被修改；完整事件回放得到相同分类。

## 9. 每个 run 必查的文件和字段

完整的正常生产 run 至少应生成：

```text
<output>/<run-id>/
  run-manifest.json
  capabilities.json
  deployment-manifest.json
  effective-profile.json
  events.jsonl
  telemetry.jsonl
  kernel-events.jsonl          # 有匹配事件时
  workload-summary.json
  serial.raw
  artifact-hashes.json
  result.json
```

`report.md/json/csv` 由 `report` 命令生成；设备 spool 和 `collection.json` 由 `collect` 生成。文档旧版中出现的 `effective-profile.yaml`/`effective-workload.json` 不应替代对实际文件的检查；若业务确实需要完整 effective workload 独立文件，应作为改进项反馈。

在启动前或协议早期发生 INFRA_ERROR 时，部分事件分类文件或 summary 可能不存在，但 `result.json` 必须明确标记 incomplete/基础设施原因；不能用缺文件的目录冒充完整 PASS。

`result.json` 必查：

- `schema_version`、producer/version、run/profile/baseline ID。
- `verdict`、Monitor `exit_code`、`dut_reasons`、`infrastructure_reasons`。
- `workload_result`、`workload_exit_code`，且与 summary/agent 进程退出一致。
- `liveness.timed_out`、`liveness.agent_final_seen`、event count。
- correctness/performance/telemetry/kernel/environment 证据。
- `artifacts.complete=true`、hash manifest、hashed file count。

事件流检查：

- 所有记录 run ID 相同、schema major 为 1。
- `seq` 从第一条开始严格连续，无重复/倒序/缺口。
- 当前 Shell agent 的 UART 原始行不携带 `crc32`；PC 接收后写入 `events.jsonl` 时补充可验证 CRC。因此线上完整性主要由 UTF-8/JSON、run ID、连续 seq 和 `agent_final` 保证，落盘事件的 CRC 必须可验证；若输入帧本身带 CRC，错误 CRC 必须是 INFRA_ERROR。
- 结束时有 summary 和 `agent_final`；`agent_final.restoration_ok=true`、`spool_complete=true`。
- `serial.raw` 不包含无过滤的持续 `dmesg` 流。

## 10. 故障注入测试

故障注入只允许在专用测试板和测试 baseline/profile 上执行，所有变更前后都要读回设备状态。

| 用例 | 注入方法 | 预期结果 |
|---|---|---|
| FI-01 CPU checksum | 板端 workload 使用错误 `--golden-checksum`，或使用版本化测试 profile。 | 原生 `CHECKSUM_FAIL`/非零退出被保留；集成判定为退出码 1。 |
| FI-02 GPU golden mismatch | 使用同尺寸但内容被修改的测试 `.rgba`，禁止覆盖批准 golden。 | 非 PASS summary 和原生退出被保留；Monitor 退出 1。 |
| FI-03 workload timeout | 测试 profile 设为 duration 大于 timeout。 | workload `TIMEOUT`，Monitor 退出 1；不能误报串口错误。 |
| FI-04 heartbeat 静默 | 保持串口有效但停止 workload heartbeat/summary。 | 退出 2，liveness 记录 watchdog 超时。 |
| FI-05 串口断开 | 运行中拔掉 PC UART 或使 COM 口失效。 | 退出 3；不能判 DUT_FAIL/PASS；保留 `serial.raw` 和 incomplete reason。 |
| FI-06 协议损坏 | 将已保存事件的一条 CRC、run ID 或 seq 改坏后 `simulate`。 | 退出 3，reason 明确为 CRC/wrong-run/sequence；同时保留已见 DUT 证据。 |
| FI-07 CPU offline/frequency | 根据 probe 的实际路径，在运行中注入 offline core 或降频。 | telemetry violation/DUT_FAIL；结束后恢复原值。 |
| FI-08 温度/throttle | 在可控热环境或测试遥测源中触发越界。 | 样本被拒绝或 run 为 DUT_FAIL，保留路径、值、单位和时间。 |
| FI-09 关键 kernel 事件 | 专用板以受控 `/dev/kmsg` 测试消息触发 `gpu hang`/`smmu fault` 规则。 | UART 只出现匹配后的 `kernel` 事件；关键事件导致 DUT_FAIL。 |
| FI-10 远端 hash | 修改一个已部署测试资产后 `run --no-deploy`。 | 运行前阻断；不能执行被篡改资产。 |
| FI-11 baseline 篡改 | 修改批准 `baseline.json` 一个字节。 | validate/run/export 返回 4；批准证据不被自动重写。 |
| FI-12 用户中止 | 正常运行中 Ctrl+C，一次在 workload 前、一次在 workload 中。 | 退出 6，仍有恢复结果和可诊断产物。若当前实现抛 traceback/无产物，按 P0 缺陷反馈。 |
| FI-13 控制链路中断 | UART 保持，断开 HDC/ADB 后恢复。 | 代理在超时/信号路径完成清理；PC 不得给假 PASS。实际分类和恢复证据需反馈。 |

原生 CPU 示例仅用于验证 workload 自身，不等同于 Monitor 端到端验收：

```powershell
hdc -t $Device shell /data/local/tmp/avs/bin/cpu-avs-workload `
  --config /data/local/tmp/avs/configs/cpu_mixed_big4.json `
  --golden-checksum 0x0000000000000000 --duration 10 --timeout 20 `
  --output-format jsonl
```

GPU 错误 golden 应以新文件名部署，例如 `gpu_vulkan_mixed.bad.rgba`，通过 `--golden-file` 指向它；不得修改或覆盖批准 baseline 关联的 golden。

## 11. 状态恢复测试

每次 CPU/GPU run 前后都保存下列值，路径以 `capabilities.json` 为准，不要硬编码 thermal zone 或 devfreq 别名：

- CPU 每核 online、governor、min/max/current frequency。
- GPU governor、min/max/current frequency、power policy、hang count。
- CPU/GPU temperature、throttle；可用时记录 voltage。
- 设备 agent/workload 残留进程和 run 目录。

分别在 PASS、checksum/golden FAIL、workload timeout、Ctrl+C、PC 串口断开、HDC/ADB 中断场景比较 before/after。通过标准：agent 修改过的设备状态恢复到原始读值；无残留 workload；恢复失败必须使 Monitor 为 INFRA_ERROR，并在 `agent_final.restoration_errors` 中记录路径和错误。

不要用 `kill -9` 验证“保证恢复”，因为 SIGKILL 无法执行任何进程清理逻辑；可以额外执行一次 SIGKILL 灾难测试，用来决定是否需要启动时恢复/看门狗机制，但应单独标记为设计改进。

## 12. UART 与长稳测试

- 使用目标平台声明的波特率和帧格式（Kirin9020 当前为 9600、8N1），关闭 workload 的 per-batch/per-frame 高频日志，遥测间隔不小于 5 秒；其他支持的串口配置需要分别验收。
- 每个 framework/profile 先连续 `--repeat 30`；再选择 CPU 和 GPU 各做一次至少 30 分钟长稳。
- `kernel-monitor=critical`、`off`、`full-local` 各执行一次。
- `full-local` 应在设备 spool 产生 `dmesg.raw`，但 UART 仍只传过滤后的 kernel 事件。
- 统计总字节、每秒峰值、heartbeat 最大间隔、telemetry 间隔、event 数、JSON/run-ID/sequence 错误、丢失/重复记录、落盘 CRC 校验结果、PC CPU/内存。

通过标准：无序号缺口、JSON/UTF-8/落盘 CRC 错误和 writer interleave；heartbeat 不超过 45 秒；正常 run 无退出码 2/3；UART 中无持续原始 dmesg；设备 spool hash 可验证。注意：落盘 CRC 证明 PC artifact 未被后续改写，不等同于 Shell UART 原始帧具有线级 CRC。

## 13. 必须反馈的日志与证据

每个失败、异常或需要调整的用例提供一个不可修改的 ZIP，目录建议如下：

```text
feedback/<package-id>/<framework>/<board-id>/<case-id>/
  case-info.json
  command.txt
  stdout.txt
  stderr.txt
  exit-code.txt
  expected-vs-actual.md
  package-sha256.txt
  capabilities.json
  pair-diagnostic.json
  deployment-manifest.json
  output/<run-id>/...
  device-spool/...
  state-before.json
  state-after.json
  native-workload.jsonl
  reproduction-notes.md
```

`case-info.json` 至少包含：

```json
{
  "case_id": "FI-05",
  "package_version": "2.0.1",
  "package_sha256": "...",
  "source_commit": "...",
  "framework": "single",
  "board_id": "BOARD_001",
  "device_serial": "DEVICE_ID",
  "bsp_kernel_driver_build": "from capabilities.json",
  "transport": "hdc",
  "pc_serial": "actual enumerated PC port",
  "baudrate": 9600,
  "started_at": "ISO-8601 with timezone",
  "command": "exact unredacted argument order, secrets removed",
  "expected_exit": 3,
  "actual_exit": 3,
  "repeat_index": 1,
  "reproducibility": "3/3"
}
```

反馈要求：

- 必须给出完整命令、退出码、stdout、stderr、时区和故障发生时间；截图不能替代文本日志。
- 优先上传整个 run 目录，不要只摘录 `result.json`。
- 不得编辑 `events.jsonl`、`serial.raw`、`artifact-hashes.json` 或 device spool；若需脱敏，保留原件并说明脱敏规则。
- 写清期望行为、实际行为、首次异常事件 `seq`/timestamp、复现次数、单/双框架差异。
- 若只在某 BSP/kernel/driver 上出现，附两份 `capabilities.json` 的 diff。
- 对状态恢复问题，附 before/after 的具体路径、写入值、读回值和权限错误。
- 对性能问题，附所有样本和 rejected 原因，不能只发平均值。
- 对串口问题，附 `pair-diagnostic.json`、`serial.raw`、事件文件、设备 UART 原始只读/可写权限、`/proc/consoles`、`stty -a`、BSP UART 配置、线材/转换器型号、主机驱动版本和波特率。
- 对温度问题，附每个 thermal zone 的 `type` 和原始 `temp` 内容，不能只附换算后的摄氏值。
- 对 GPU capability 问题，附 hang/reset/fault counter 的真实候选路径、权限、空闲值以及一次可控恢复/故障前后的值；在没有证据前不要猜路径或把它改成 optional。

## 14. 代码调整定位指南

| 证据特征 | 优先检查模块 |
|---|---|
| 路径、外部配置、冻结包资源错误 | `src/path_resolver.py`、`vmin_judge.spec`、`scripts/build.ps1` |
| HDC/ADB 命令、push/pull/hash 失败 | `src/transport.py`、`src/deployment.py` |
| sysfs/thermal/debugfs 路径或单位错误 | `config/platforms/kirin9020.yaml`、`src/platform_probe.py` |
| run manifest/environment/telemetry 配置错误 | `src/run_orchestrator.py`、profile YAML |
| UART 序号、run ID、粘包/断包 | `src/events.py`、`device/avs_device_agent.sh` |
| DUT/infra 分类、阈值或原生退出码丢失 | `src/policy_engine.py` |
| golden/calibration/cohort/rejection 问题 | `src/qualification.py` |
| baseline 审批、hash、导入导出 | `src/baselines.py` |
| 产物缺失、不完整或 hash 错误 | `src/artifact_store.py`、`src/cli_commands.py` |
| 状态未恢复、agent 退出不一致 | `device/avs_device_agent.sh`、`src/run_orchestrator.py` |

## 15. 最终发布门槛

以下条件全部满足后才建议进入批量设备测试：

- 实际发布 EXE 在干净 PC、非源码目录和只读安装目录通过 REL-01～REL-05。
- 单框架和双框架均通过 full probe，必需接口、单位和权限明确，无 `required_missing`。
- 首次/重复部署 hash 正确且幂等，清理边界安全。
- 至少一个批准 CPU baseline 和一个批准 GPU baseline 来自不少于两块已知良品板，校准 accepted sample 不少于 20。
- 两种 framework 的 CPU/GPU 基本 PASS 各连续 30 次，无 INFRA_ERROR/SILENT_FAILURE。
- FI-01～FI-13 的预期分类、原生退出码、恢复和产物均符合规则；未实现的用户中止/多板采样体验必须形成明确缺陷或改进项。
- 30 分钟 UART 长稳无 JSON/run-ID/seq 错误，PC 落盘 CRC 全部可验证，raw dmesg 不进入 UART。
- 每个 run 都有完整 `result.json` 和可验证 `artifact-hashes.json`；失败反馈包足以离线 `simulate` 和复现。

任何缺少原始日志、缺少 package hash、缺少 exact command、缺少设备版本/能力信息或产物 hash 不一致的结果，都不能作为关闭代码问题的依据。

## 附录 A：命令到代码行为的对应关系

所有命令都由 `main.py` 解析；全局参数必须位于子命令前。表中“CMD 输出”指默认 `--log-level warning` 下的 stdout/stderr，不代表 UART 或产物内容被删除。

| 命令 | 主要代码入口 | 实际行为 | CMD/产物预期 |
|---|---|---|---|
| `validate` | `src/cli_commands.py::cmd_validate_v2`、`src/path_resolver.py` | 只解析配置/schema/资源/baseline；`--offline` 不访问设备；记录 profile/platform 的绝对路径和 SHA-256。 | 最终 `valid/checked/resolved_configs/errors`；无 traceback。 |
| `list-profiles` | `cmd_list_profiles_v2` | 从最终解析到的 profile 目录枚举配置。 | 最终列表包含 path，可用于发现错误配置根。 |
| `probe` | `_probe`、`src/platform_probe.py::PlatformProbe` | 连接设备并实时读取接口；独立 `--full` 扫 CPU+GPU，live profile 只扫目标域；随后检查 Shell/hash/dmesg/taskset。 | 最终 probe 对象和 `capabilities.json`；含 `platform_config` provenance。 |
| `pair` | `main.py::cmd_pair`、`src/serial_port_manager.py` | 显式参数优先；先打开/清空 PC COM，再经 HDC/ADB 多次写唯一 marker，接收可分片字节并验证，保存配对。不会自动改设备 `stty`。 | 成功/分类错误摘要；详细有界证据在 `pair-diagnostic.json`，正常 marker 不回显。 |
| `deploy` | `cmd_deploy`、`src/deployment.py::DeploymentManager` | 规划 agent/workload/workload config/shader/golden，远端 hash 相同则跳过；可安全清理旧 manifest 中且限定根目录内的资产。 | 最终 manifest 路径；清单逐资产记录 pushed/unchanged/hash。平台 YAML 不部署。 |
| `smoke` | `cmd_smoke`、`_execute_live_qualification`、`RunManifestBuilder`、`RunOrchestrator` | 使用 baseline-free 临时 manifest 复用目标域 probe、部署、agent/workload、UART 判断和落盘；生成的 correctness reference 明确丢弃；PASS spool 保留供 collect。 | 最终对象含 `minimum_closed_loop` 和唯一/重复 run 结果；不创建 production golden/baseline。 |
| `golden` | `cmd_golden`、`_execute_live_qualification`、`GoldenService` | 不足的 run 用统一 live 事务补齐；每次必须 PASS 且恰有一个 golden；随后聚合 checksum/readback 和 fingerprint。 | 成功时给 qualification ID/manifest/hash；首个 live 失败立即给具体 `result.json` 路径。 |
| `calibrate` | `cmd_calibrate`、`CalibrationService` | 从已有 run 或 live run 构造样本，按温度/遥测/throttle/environment 拒绝不合格样本，生成 draft baseline。 | 最终 baseline ID/draft/proposal；不会自动批准。 |
| `baseline` | `BaselineRegistry` | 创建后批准 evidence 不可变；批准/弃用状态与审计分离；导入导出校验 hash。 | 最终 ID/status/hash/bundle；篡改返回配置错误。 |
| `run` / `execute` | `cmd_run`、`RunManifestBuilder`、`RunOrchestrator`、`PolicyEngine` | 两个名字进入同一 v2 事务；不会隐式 pair。依次解析配置/基线、profile probe、部署、建 manifest、COM-before-agent、UART 判断、落盘、PASS spool 清理。 | 仅最终批次对象；失败 run 增加精简 `errors`，完整 reasons 在 `result.json`。 |
| `monitor` | `cmd_monitor_events` | 直接监听已存在的 JSONL 生产者，自动从第一帧识别 run ID，校验协议并存证；没有 manifest/baseline 策略上下文。 | `NOT_EVALUATED` 诊断结果，绝不签发 DUT PASS。 |
| `collect` | `cmd_collect` | 拉取一个精确 run 的 spool；有且仅有一个 hash manifest 时逐文件验证；验证成功且未 `--keep-remote` 才删除远端。 | `collection.json`；hash/路径异常退出 3 并保留远端。 |
| `report` | `cmd_report` | 从既有 `result.json` 重建 markdown/json/csv，不重跑判断。 | 新报告与原 result 一致，原 run 不变。 |
| `simulate` | `cmd_simulate`、`RunOrchestrator.evaluate_stream` | 用保存的 framed events 或 raw serial 进入同一个事件解码/判断路径；可选 realtime 时间重放。 | 新 replay run 目录；协议/分类应可复现。 |

### A.1 normal `run` 的实际事务边界

```text
profile/platform/baseline 解析
  -> 目标域实时 probe + required tools
  -> 资产 hash 部署或 --no-deploy 校验
  -> 构建唯一 run manifest / agent argv
  -> 打开并清空 PC COM
  -> HDC/ADB 启动 Shell agent + workload
  -> UART 事件进入 EventDecoder / PolicyEngine / ArtifactStore
  -> summary + restore + agent_final
  -> 最终 result.json 与 CMD 结果
  -> 仅 PASS：精确 spool 自动删除（除非 --keep-device-spool）
```

不属于 normal `run` 的隐式行为：自动 pair、生成 golden、校准/批准 baseline、全平台无关域扫描、把 run manifest/platform YAML 推给 agent、把正常 UART 明细复制到 CMD。

## 附录 B：接口、数据方向与判断行为

### B.1 PC 配置与路径接口

| 接口 | 方向 | 代码行为 | 现场检查 |
|---|---|---|---|
| `--config-dir` | PC 文件 -> PC | 优先于 cwd/EXE/bundle 同名输入；兼容 `<root>/config/...` 与 `<root>/...`。 | `validate.resolved_configs` 和 `probe.platform_config` 的 path/hash。 |
| `--output-dir` | PC 写 | 所有 probe/qualification/run/report 产物写到该根。 | EXE/bundle 目录无新增文件。 |
| `--state-dir` | PC 持久写 | 保存 `pairing.conf`、pair 诊断和 baseline registry。 | 换 `$State` 后不会意外复用旧 pairing/baseline。 |
| `--device-root` | PC 规划 -> 设备路径 | 生成 POSIX remote path；默认 `/data/local/tmp/avs`。 | deploy/spool/cleanup 都在此根内。 |

### B.2 HDC/ADB 和设备 agent 接口

- Transport 把设备命令作为 argv 调用，并区分主机工具失败与远端 Shell 退出码；不能把 `/bin/sh: ... not found` 且 host return code 0 误报为 capability available。
- PC 不要求设备解析 JSON manifest。`RunManifestBuilder` 把已经解析的环境、遥测、kernel rule、超时和 workload argv 转成 agent 参数，Shell agent 只消费固定选项和 `--` 后的 workload argv。
- agent 只允许一个 `emit_event` 写者把同一 JSON 行依次追加到设备 `events.jsonl` 和 UART，workload/telemetry/kernel 逻辑不直接并发打开 UART。
- workload stdout/stderr 必须是支持的原生 JSON 对象。无法识别 `type` 的行被转换为 `WORKLOAD_OUTPUT_INVALID`，错误事件中保留最多 256 字符的原始诊断，避免无原因失败。
- `kernel-monitor=critical`/`full-local` 在设备侧先收集再按版本化规则过滤和去重；只有匹配事件进 UART。`full-local` 可保留 `kernel.raw`，不代表原始 `dmesg` 可进入 UART。

### B.3 UART 事件类型

UART 每行一个 UTF-8 JSON 对象，最低字段：

```json
{
  "schema_version": 1,
  "run_id": "unique-run-id",
  "seq": 1,
  "timestamp_ms": 0,
  "source": "agent|cpu-workload|gpu-workload|cpu-telemetry|gpu-telemetry|kernel",
  "type": "agent_start|environment|start|heartbeat|batch|verify|golden|telemetry|kernel|error|violation|summary|agent_final",
  "payload": {}
}
```

| 事件 | 设备侧行为 | PC 判断行为 | CMD 行为 |
|---|---|---|---|
| `agent_start` | 报 agent/protocol/baud，seq 从 1 开始。 | 标记 agent 已启动并开启 workload liveness。 | 正常不打印。 |
| `environment` | 每个成功 readback/restore 报 path/requested/actual/required/success。 | 作为可审计环境证据保存。 | 正常不打印。 |
| agent `error` | apply/readback/restore 失败报 phase/path/requested/actual/error_code。 | required 设置失败或恢复失败归基础设施原因。 | 最终失败对象复制精简字段。 |
| workload `start/heartbeat/batch/verify/golden` | 原生 workload JSON 被包入统一 envelope。 | 这些 workload 事件刷新 workload activity；telemetry 不刷新。 | 正常不打印。 |
| workload `error/violation` | 保留原生 error code/line。 | 归 DUT 或 workload reason，保留 workload exit。 | 最终失败对象显示精简原因。 |
| `telemetry` | 每 5 秒按已解析 path 采样单 metric；温度按 parser 归一化。 | required presence、阈值、性能/环境策略；不掩盖 heartbeat 静默。 | 正常不打印。 |
| `kernel` | 只发匹配并有界去重的规则事件。 | critical severity 可判 DUT_FAIL。 | 只在最终 reason 中摘要。 |
| `summary` | workload 原生最终结果。 | 停止 workload heartbeat watchdog，保存 `workload-summary.json`。 | 不逐条打印，最终 verdict 汇总。 |
| `agent_final` | 报 workload_exit、summary_seen、timed_out、restoration_ok、spool_complete。 | 必须存在并与 HDC/ADB agent 进程退出一致，否则 INFRA_ERROR。 | 只反映到最终结果。 |

Shell agent 原始 UART 帧不含 `crc32`；PC 的 `ArtifactStore` 在规范化写入 `events.jsonl`、`telemetry.jsonl`、`kernel-events.jsonl` 时补计算 CRC。协议受损后，PC 保留已确定的 INFRA_ERROR，停止把后续字节当可信事件，但继续排空并保存原始串口，直到 agent 命令结束或排空窗口结束。

### B.4 设备 sysfs 与环境行为

| 语义接口 | 配置候选/来源 | agent/PC 行为 | 必查结果 |
|---|---|---|---|
| CPU policy max | `.../cpufreq/policy*/cpuinfo_max_freq` 或实际 probe 最大频率记录 | `RunManifestBuilder` 按 `policyN`/CPU suffix 把每个环境 action 绑定到自己的实例；不会用一个最大值覆盖所有 cluster。 | environment requested/actual 与 DEV-INFO policy 表一致。 |
| CPU governor/online | probe 的实际 `scaling_governor`、`cpu*/online` | 写入前保存旧值，写后 readback，结束逐项 restore。 | readback/restore 事件齐全；CPU0 implicit online 不伪造可写路径。 |
| GPU hang count | v500 优先、v350 回退 | 独立 full probe 扫描候选；GPU profile 设为 required，CPU profile 不触碰。 | path、权限、数值及运行前后计数。 |
| Thermal | thermal zone `type` + `temp`，`temperature_unit:auto` | 每个读值独立判断 degree/millidegree，保留 probe raw/normalized/valid 信息；agent 接收最终 parser。 | `30` 约为 30 °C，`31074` 约为 31.074 °C，不得变成 0.03/31074 °C。 |
| GPU power/frequency/utilization | `kirin9020.yaml` 当前候选 | probe 解析实际 path，live manifest 只携带已选择路径。 | capabilities path 与 UART telemetry path 一致。 |

### B.5 产物和设备 spool

PC run 目录是判定主记录；设备 spool 是 UART 丢失/控制链路异常时的后备记录。两者的 `events.jsonl` 语义相同但哈希清单独立：

| 位置 | 产生者 | 生命周期 |
|---|---|---|
| `$Out/<run-id>/serial.raw` | PC 串口读取 | 总是尽量保存，包括协议错误后的原始尾部；不可编辑。 |
| `$Out/<run-id>/events.jsonl` | PC ArtifactStore | 仅包含成功解码的规范化事件，带落盘 CRC。 |
| `$Out/<run-id>/result.json` | PC PolicyEngine/RunOrchestrator | 最终 verdict、退出码、DUT/infra reasons、liveness、workload exit。 |
| `$RemoteRoot/runs/<run-id>/spool/events.jsonl` | Shell agent | UART 同源备份；PASS normal run 默认删除，失败默认保留。 |
| `.../spool/artifact-hashes.json` | Shell agent | 当前至少校验 device events 文件；`collect --verify-hashes` 逐项验证。 |

## 附录 C：关键预期输出示例

以下示例只说明字段关系；path、hash、run ID 和数值以现场为准。

### C.1 批准配置被正确选中

```json
{
  "valid": true,
  "resolved_configs": [
    {
      "kind": "platform",
      "name": "kirin9020",
      "path": "D:\\approved-config\\platforms\\kirin9020.yaml",
      "sha256": "same-as-Get-FileHash"
    }
  ],
  "errors": []
}
```

Probe 还应包含：

```json
{
  "platform_config": {
    "path": "D:\\approved-config\\platforms\\kirin9020.yaml",
    "sha256": "same-as-validate",
    "external_override": true
  }
}
```

### C.2 环境 readback 成功与失败

```json
{"type":"environment","payload":{"phase":"readback","path":"/sys/devices/system/cpu/cpufreq/policy4/scaling_max_freq","requested":"2050000","actual":"2050000","required":true,"success":true}}
```

```json
{"type":"error","payload":{"origin":"agent","error_code":"ENVIRONMENT_READBACK_FAILED","phase":"readback","path":"/sys/.../scaling_max_freq","requested":"2050000","actual":"1800000","required":true}}
```

第二种情况的 CMD 最终对象应含精简 `errors`，至少保留 code/phase/path/requested/actual；完整 envelope、seq 和时间在 `events.jsonl`/`result.json`。

### C.3 normal run 成功/失败 CMD

成功示例：

```json
{
  "exit_code": 0,
  "repeat": 1,
  "runs": [
    {"exit_code": 0, "result": "...\\result.json", "run_id": "CPU-001", "verdict": "PASS"}
  ]
}
```

失败示例：

```json
{
  "exit_code": 3,
  "repeat": 1,
  "runs": [
    {
      "run_id": "CPU-001",
      "verdict": "INFRA_ERROR",
      "exit_code": 3,
      "result": "...\\result.json",
      "device_spool": "retained",
      "errors": [{"code": "ENVIRONMENT_READBACK_FAILED", "phase": "readback", "path": "/sys/...", "requested": "2050000", "actual": "1800000"}]
    }
  ]
}
```

### C.4 当前两类问题的快速分流

| 现场现象 | 先看什么 | 判定方向 | 必须回传 |
|---|---|---|---|
| 改了 approved-config 但仍识别不到 v500 | DEV-00 hash、validate path/hash、probe platform_config | path/hash 错是 PC 配置选择；正确但候选仍旧是配置/schema 缺陷 | 三份 JSON/text + 实际 YAML |
| probe 有 v500，GPU golden 仍报 hang missing | full capabilities 和失败 run capabilities/manifest | profile scope 或 live manifest 构建缺陷 | 两份 capabilities、run manifest、CMD error |
| golden 看似“卡住”，CMD 无进度 | 最新 `golden-*-*` 的 serial.raw/events/result、设备 spool、HDC agent 进程 | 无 UART 是链路/agent；只有 telemetry 无 workload 是 SILENT；协议损坏是 INFRA | 整个 run、spool、ps、精确时间 |
| CPU environment requested 全是 2508000 | DEV-INFO cpufreq policy 表与 environment events | per-policy 映射回归 | policy 表、capabilities、events |
| workload 失败但 CMD 没原因 | CMD JSON 与 result reasons/workload summary | 精简 reason 映射缺陷或原生输出无效 | stdout/stderr、result、summary、设备 workload log |
| PASS 后设备存储持续增长 | CMD run ID、远端 runs/spool、`--keep-device-spool` 使用情况 | 自动清理/精确路径保护问题 | run 输出、远端目录、collection 记录 |
