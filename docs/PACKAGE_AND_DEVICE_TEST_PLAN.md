# Vmin Judge 打包与设备验收测试文档

状态：适用于 `vmin_judge 2.0.0`、配置/事件/清单/基线/结果 schema v1

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
| 3 | INFRA_ERROR | 串口断开、CRC/序号错误、代理异常、收集/恢复/哈希失败。 |
| 4 | INVALID_CONFIGURATION | 参数、配置、schema、路径、基线或 fingerprint 错误。 |
| 5 | UNSUPPORTED | 必需设备接口、工具或兼容基线缺失。 |
| 6 | USER_ABORT | 用户中止，仍应尝试清理和恢复。 |

原生 workload 的退出码与 Monitor 退出码不是同一字段。CPU/GPU workload 的非 PASS 结果必须保留在 `result.json` 的 `workload_result` 和 `workload_exit_code` 中，不能只保留 Monitor 的退出码。

## 2. 测试范围与优先级

| 优先级 | 测试域 | 必须验证的功能 |
|---|---|---|
| P0 | 发布包 | 独立 EXE、版本/schema、资源完整、非源码目录运行、只读安装目录、稳定退出码。 |
| P0 | 设备探测 | HDC/ADB、CPU/GPU/sysfs/debugfs/thermal、Python 3、SHA-256、`dmesg`、`taskset`。 |
| P0 | 部署 | 推送、权限、远端 SHA-256、重复部署幂等、受控清理。 |
| P0 | 串口协议 | 单写者、JSONL、run ID、连续序号、CRC、heartbeat、summary、`agent_final`。 |
| P0 | CPU/GPU | golden、校准、基线审批、批量 PASS、原生失败结果保留。 |
| P0 | 状态安全 | PASS、FAIL、超时、中止和控制链路异常后的 governor/frequency/online 状态恢复。 |
| P1 | 故障注入 | checksum/golden mismatch、超时、遥测违规、关键内核事件、串口损坏。 |
| P1 | 产物 | 本地/设备 spool 收集、哈希校验、报告重建、离线回放。 |
| P1 | 长稳与带宽 | 115200 波特率下无丢帧/乱序/CRC 错误，原始 `dmesg` 不进入 UART。 |

## 3. 测试环境与变量

### 3.1 必需环境

- Windows 测试 PC，能够访问 HDC 或 ADB，并有可用的 PC UART（例如 `COM8`）。
- 至少一块单框架设备、一块双框架设备；生产基线校准至少覆盖两块代表性已知良品板。
- 设备镜像允许读取必需 sysfs，并允许设置 governor/frequency/online 状态。
- 当前设备代理要求板端有 `python3`。如果生产镜像不提供 Python，应先替换为原生代理，不能跳过该检查。
- 已构建的 HarmonyOS CPU/GPU workload 和 Vulkan SPIR-V：
  - `tools/cpu-avs-workload`
  - `tools/gpu-avs-workload`
  - `tools/shaders/vulkan/fullscreen.vert.spv`
  - `tools/shaders/vulkan/workload.frag.spv`

### 3.2 PowerShell 测试变量

所有全局参数必须放在子命令之前。

```powershell
$Exe = 'D:\release\vmin_judge.exe'
$PackageId = 'vmin-judge-2.0.0-YYYYMMDD'
$Device = 'DEVICE_ID'
$BoardId = 'BOARD_001'
$Framework = 'single'       # single 或 dual
$PcSerial = 'COM8'
$Out = "D:\MonitorTest\$PackageId\$Framework\$BoardId\output"
$State = "D:\MonitorTest\$PackageId\$Framework\$BoardId\state"
$Evidence = "D:\MonitorTest\$PackageId\$Framework\$BoardId\evidence"
New-Item -ItemType Directory -Force -Path $Out, $State, $Evidence | Out-Null
```

本文以 HDC 为例。ADB 环境将 `--transport hdc` 改为 `--transport adb`。有多个设备时必须传 `--device`，不得依赖自动选择。

## 4. 打包前和发布包测试

### REL-01：源码离线回归

```powershell
python -m unittest discover -s tests -v *> "$Evidence\REL-01-unittest.txt"
$LASTEXITCODE
```

通过标准：50 个测试全部通过，退出码为 0。当前仓库已在 2026-08-27 验证为 50/50 通过；发布前仍需在实际构建环境重跑。

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
- 版本为 `vmin_judge 2.0.0`，并报告 `config=1 event=1 manifest=1 baseline=1 result=1`。
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

通过标准：EXE/临时 bundle 目录无写入；外部配置按“绝对路径、当前目录、`--config-dir`、EXE 外部覆盖、内置资源”的顺序解析；相对输出只写入 `$Out`。

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

设备代理接口：

```text
avs-device-agent --manifest RUN_MANIFEST --uart /dev/ttyAMA0 \
  --baudrate 115200 [--spool-dir DIR] [--dry-run]
```

事件接口为每行一个 UTF-8 JSON，关键字段是 `schema_version`、`run_id`、`seq`、`timestamp_ms`、`source`、`type`、`payload`、`crc32`。正常顺序至少应能看到 `agent_start`、环境/能力事件、`start`、多个 `heartbeat`/`telemetry`、`summary` 和最后的 `agent_final`。

## 6. 设备基础与接口探测

### DEV-01：HDC/ADB 和板端工具

先用原生命令确认控制链路，仅作辅助证据：

```powershell
hdc -t $Device list targets
hdc -t $Device shell python3 --version
hdc -t $Device shell sha256sum --help
hdc -t $Device shell dmesg --help
hdc -t $Device shell taskset --help
```

如果设备只有 `toybox sha256sum`，Monitor 会自动回退。原生命令的 stdout、stderr 和退出码都要保存。

### DEV-02：完整 probe

在单框架和双框架镜像各执行一次：

```powershell
& $Exe --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  probe --platform kirin9020 --full --refresh `
  *> "$Evidence\DEV-02-probe.json"
$ProbeExit = $LASTEXITCODE
Copy-Item "$Out\probes\$Device\capabilities.json" `
  "$Evidence\DEV-02-capabilities-$Framework.json"
```

通过标准：`$ProbeExit=0`、`supported=true`、`required_missing=[]`。重点核对：

- CPU：每核 frequency/min/max/governor/online、`/proc/stat` utilization、按 thermal `type` 匹配的温度。
- GPU：frequency/min/max/governor/utilization/power policy/hang count、按 thermal `type` 匹配的温度。
- 工具：`device.python3`、`device.sha256sum` 可用；CPU profile 运行时 `device.taskset` 必需；kernel monitor 开启时 `device.dmesg` 必需。
- 每个 metric 都有实际路径、单位、值和 provenance，不允许只有“可用”布尔值。
- 单/双框架可以使用不同真实路径，但语义、单位和必需能力必须等价。把两个 `capabilities.json` 做字段级 diff，并注明所有差异。

缺少必需接口时预期退出码为 5。不要通过删除 profile 的 required 项来“通过”验收。

### DEV-03：串口配对和诊断监听

配对时只连接一块目标设备，避免旧 `pair` 接口选择歧义：

```powershell
& $Exe --state-dir $State pair --channel hdc --device-port /dev/ttyAMA0 `
  --pc-port $PcSerial --baudrate 115200 --timeout 3 --verify `
  *> "$Evidence\DEV-03-pair.txt"
$PairExit = $LASTEXITCODE
```

随后进行外部启动/串口 bring-up 时可使用：

```powershell
& $Exe --pc-serial $PcSerial --baudrate 115200 --output-dir $Out --state-dir $State `
  monitor --save-raw --timeout 60
```

通过标准：pair 退出码 0；monitor 能识别连续事件。`monitor` 是诊断命令，结果必须是 `NOT_EVALUATED`，不能输出 DUT PASS。

### DEV-04：部署与幂等

```powershell
& $Exe --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  deploy --target all --verify-hashes *> "$Evidence\DEV-04-deploy-first.json"
Copy-Item "$Out\deployment-manifest.json" "$Evidence\DEV-04-manifest-first.json"

& $Exe --transport hdc --device $Device --output-dir $Out --state-dir $State --json `
  deploy --target all --verify-hashes *> "$Evidence\DEV-04-deploy-second.json"
Copy-Item "$Out\deployment-manifest.json" "$Evidence\DEV-04-manifest-second.json"

hdc -t $Device shell /data/local/tmp/avs/bin/avs-device-agent --version `
  *> "$Evidence\DEV-04-agent-version.txt"
```

通过标准：首次部署所有必需项为 `pushed` 且远端 hash 与本地一致；第二次部署为 `unchanged`，不重复传输；agent 报告 `0.1.0 protocol 1`。`--clean-stale` 只允许删除先前 manifest 记录且位于 `/data/local/tmp/avs` 内的文件，需要单独做一次安全边界测试。

## 7. CPU/GPU 资格认证与基线

当前 profile 中 `baseline: null`，所以不能直接执行生产 `run --baseline auto`。必须先生成 golden、完成多板校准并批准 baseline。

### QUAL-01：CPU golden

```powershell
& $Exe --transport hdc --device $Device --pc-serial $PcSerial `
  --output-dir $Out --state-dir $State --json `
  golden cpu --profile cpu_mixed_big4 --runs 10 --known-good --board-id $BoardId `
  *> "$Evidence\QUAL-01-cpu-golden.json"
$LASTEXITCODE
```

通过标准：10 次均为 PASS；每次恰有一个 `golden` 事件；checksum 完全一致；环境为 cores 4–7 online、performance governor、platform max frequency、35–60 °C；golden manifest 包含 workload/config/profile hash 和板号。正式审批前应使用已独立确认的 `--accept-checksum` 再做一次绑定检查。

### QUAL-02：GPU golden

```powershell
& $Exe --transport hdc --device $Device --pc-serial $PcSerial `
  --output-dir $Out --state-dir $State --json `
  golden gpu --profile gpu_vulkan_mixed --runs 10 --known-good --board-id $BoardId `
  --readback-name gpu-golden.rgba *> "$Evidence\QUAL-02-gpu-golden.json"
$LASTEXITCODE
```

通过标准：10 次 raw readback 字节数和 SHA-256 完全一致；shader/workload/config hash、驱动/BSP/板号均被记录；`verify_mode` 不是 `none`；原始 `.rgba` 按二进制缓冲区保存，不当作图片解码。

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

& $Exe --output-dir $Out --state-dir $State --json calibrate cpu `
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
& $Exe --transport hdc --device $Device --pc-serial $PcSerial `
  --output-dir $Out --state-dir $State --json run `
  --profile cpu_mixed_big4 --baseline kirin9020-cpu-mixed-big4-v1 `
  --repeat 3 --run-id "CPU-$Framework-$BoardId" --kernel-monitor critical `
  *> "$Evidence\RUN-01-cpu.json"
$CpuExit = $LASTEXITCODE

& $Exe --transport hdc --device $Device --pc-serial $PcSerial `
  --output-dir $Out --state-dir $State --json run `
  --profile gpu_vulkan_mixed --baseline kirin9020-gpu-vulkan-mixed-v1 `
  --repeat 3 --run-id "GPU-$Framework-$BoardId" --kernel-monitor critical `
  *> "$Evidence\RUN-01-gpu.json"
$GpuExit = $LASTEXITCODE
```

通过标准：两个退出码均为 0；每次 run 的 verdict 为 PASS；无 DUT/infra reasons；summary 和 `agent_final` 都存在；required telemetry 每类至少有一个实例；环境 readback 匹配；退出后状态恢复。

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
- CRC 全部可验证；任何 CRC/UTF-8/JSON/截断错误必须是 INFRA_ERROR。
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

- 固定 115200、8N1，关闭 workload 的 per-batch/per-frame 高频日志。
- 每个 framework/profile 先连续 `--repeat 30`；再选择 CPU 和 GPU 各做一次至少 30 分钟长稳。
- `kernel-monitor=critical`、`off`、`full-local` 各执行一次。
- `full-local` 应在设备 spool 产生 `dmesg.raw`，但 UART 仍只传过滤后的 kernel 事件。
- 统计总字节、每秒峰值、heartbeat 最大间隔、telemetry 间隔、event 数、CRC/sequence 错误、丢失/重复记录、PC CPU/内存。

通过标准：无序号缺口、CRC/JSON/UTF-8 错误和 writer interleave；heartbeat 不超过 45 秒；正常 run 无退出码 2/3；UART 中无持续原始 dmesg；设备 spool hash 可验证。

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
  "package_version": "2.0.0",
  "package_sha256": "...",
  "source_commit": "...",
  "framework": "single",
  "board_id": "BOARD_001",
  "device_serial": "DEVICE_ID",
  "bsp_kernel_driver_build": "from capabilities.json",
  "transport": "hdc",
  "pc_serial": "COM8",
  "baudrate": 115200,
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
- 对串口问题，附 `serial.raw`、事件文件、线材/转换器型号、COM 驱动版本和波特率。

## 14. 代码调整定位指南

| 证据特征 | 优先检查模块 |
|---|---|
| 路径、外部配置、冻结包资源错误 | `src/path_resolver.py`、`vmin_judge.spec`、`scripts/build.ps1` |
| HDC/ADB 命令、push/pull/hash 失败 | `src/transport.py`、`src/deployment.py` |
| sysfs/thermal/debugfs 路径或单位错误 | `config/platforms/kirin9020.yaml`、`src/platform_probe.py` |
| run manifest/environment/telemetry 配置错误 | `src/run_orchestrator.py`、profile YAML |
| UART CRC、序号、run ID、粘包/断包 | `src/events.py`、`device/avs_device_agent.py` |
| DUT/infra 分类、阈值或原生退出码丢失 | `src/policy_engine.py` |
| golden/calibration/cohort/rejection 问题 | `src/qualification.py` |
| baseline 审批、hash、导入导出 | `src/baselines.py` |
| 产物缺失、不完整或 hash 错误 | `src/artifact_store.py`、`src/cli_commands.py` |
| 状态未恢复、agent 退出不一致 | `device/avs_device_agent.py`、`src/run_orchestrator.py` |

## 15. 最终发布门槛

以下条件全部满足后才建议进入批量设备测试：

- 实际发布 EXE 在干净 PC、非源码目录和只读安装目录通过 REL-01～REL-05。
- 单框架和双框架均通过 full probe，必需接口、单位和权限明确，无 `required_missing`。
- 首次/重复部署 hash 正确且幂等，清理边界安全。
- 至少一个批准 CPU baseline 和一个批准 GPU baseline 来自不少于两块已知良品板，校准 accepted sample 不少于 20。
- 两种 framework 的 CPU/GPU 基本 PASS 各连续 30 次，无 INFRA_ERROR/SILENT_FAILURE。
- FI-01～FI-13 的预期分类、原生退出码、恢复和产物均符合规则；未实现的用户中止/多板采样体验必须形成明确缺陷或改进项。
- 30 分钟 UART 长稳无 CRC/seq/JSON 错误，raw dmesg 不进入 UART。
- 每个 run 都有完整 `result.json` 和可验证 `artifact-hashes.json`；失败反馈包足以离线 `simulate` 和复现。

任何缺少原始日志、缺少 package hash、缺少 exact command、缺少设备版本/能力信息或产物 hash 不一致的结果，都不能作为关闭代码问题的依据。
