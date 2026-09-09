# CPU/GPU 双板校验与按需取证

本手册对应 verification contract v2。以下是待执行的实机验收流程，不是已完成的测试记录。
Monitor 不调压、不修改 governor/频率/online/affinity。先在已确认的额定电压和受控温度下完成资格化，再由实验室既有流程逐点调压。

## 升级前提

- 同时更新 Monitor、设备 agent 和 CPU/GPU workload。workload `--version` 为 2.1.0，`--capabilities` 必须报告 contract_version=2。
- 重新构建设备架构二进制，放入 profile 指定的本地路径；GPU 仍使用已经验证的 Vulkan/shader 构建流程。本仓库没有替你完成设备交叉编译。
- 所有配置改变都要重新 deploy。binary/config/shader 指纹改变后，旧 golden/baseline 不能继续使用，必须重新生成。
- 每次 run 会只读检查所需文件 SHA-256 及 workload capabilities；不会补文件。GPU golden-image 运行必须显式提供 `--golden` 或 `--baseline`，从而核验 readback 的设备哈希。
- 正式正确性/性能资格化采用 `verify_interval=1`、`success_log_interval=60`、逐 batch/frame 日志关闭。成功日志另有最多每秒一次的限速；失败不受此限速影响。

新增 17 组配置见 [PROFILE_CATALOG.md](PROFILE_CATALOG.md)。首先完成以下 mixed 流程，再分别对 CPU 四个后端和 GPU 四个 shader 的资格化 profile 重复整个流程；每个 profile 使用独立 golden 和 baseline。

## 一次覆盖两板和两种 workload

PowerShell 示例显式填写两板设备 ID、串口和 UART。每块板测试前确认电压、频率策略和温度；不能把 BOARD-A 的串口映射直接当成 BOARD-B 的映射。

```powershell
$MON = 'D:\Monitor-release\vmin_judge.exe' # 换成当前发布包
$OUT = 'D:\AVS-results'
$STATE = 'D:\AVS-state'
$SESSION = Get-Date -Format yyyyMMdd-HHmmss
$Boards = @(
    @{Id='BOARD-A'; Device='<A的HDC设备ID>'; Com='<A的COM口>'; Uart='/dev/ttyHW0'},
    @{Id='BOARD-B'; Device='<B的HDC设备ID>'; Com='<B的COM口>'; Uart='/dev/ttyHW0'}
)
$Targets = @(
    @{Name='cpu'; Profile='cpu_qualification_kirin9030'; Smoke='cpu_smoke_kirin9030'},
    @{Name='gpu'; Profile='gpu_qualification_kirin9030'; Smoke='gpu_smoke_kirin9030'}
)
$Common = @('--output-dir',$OUT,'--state-dir',$STATE,'--json')
function Invoke-Monitor([string[]]$Arguments) {
    $Response = & $MON @Common @Arguments
    if ($LASTEXITCODE -ne 0) { throw ($Response -join "`n") }
    return ($Response -join "`n" | ConvertFrom-Json)
}
$Goldens = @{}
$Samples = @{}
$RunsPerBoard = 1 # 仅两板功能闭环；生产建议每板30次，不降低默认最低接受数

foreach ($Target in $Targets) {
    $GoldenSources = @()
    foreach ($Board in $Boards) {
        $Link = @('--transport','hdc','--device',$Board.Device)
        $Serial = @('--pc-serial',$Board.Com,'--device-uart',$Board.Uart)
        # 新平台先按主测试计划完成 probe、pair、relay probe。
        Invoke-Monitor ($Link + @('deploy','--profile',$Target.Smoke))
        Invoke-Monitor ($Link + @('verify-deployment','--profile',$Target.Smoke))
        Invoke-Monitor ($Link + $Serial + @('run','--profile',$Target.Smoke,
            '--test-id',"SMOKE-$($Target.Name)-$($Board.Id)-$SESSION"))
        Invoke-Monitor ($Link + @('deploy','--profile',$Target.Profile))
        Invoke-Monitor ($Link + @('verify-deployment','--profile',$Target.Profile))
        $Capture = Invoke-Monitor ($Link + $Serial + @('golden',$Target.Name,
            '--profile',$Target.Profile,'--board-id',$Board.Id,'--known-good','--runs','1',
            '--qualification-id',"GOLD-$($Target.Name)-$($Board.Id)-$SESSION"))
        $GoldenSources += @('--run-dir',"$($Board.Id)=$($Capture.source_runs[0])")
    }
    # CPU checksum必须相同；GPU两板raw readback必须逐字节相同。
    $Merged = Invoke-Monitor (@('golden',$Target.Name,'--profile',$Target.Profile,
        '--board-id','BOARD-A','--known-good','--runs','2',
        '--qualification-id',"GOLD-$($Target.Name)-2BOARD-$SESSION") + $GoldenSources)
    $Goldens[$Target.Name] = $Merged.golden_manifest
    $Samples[$Target.Name] = @()
    foreach ($Board in $Boards) {
        $Link = @('--transport','hdc','--device',$Board.Device)
        $Serial = @('--pc-serial',$Board.Com,'--device-uart',$Board.Uart)
        Invoke-Monitor ($Link + @('deploy','--profile',$Target.Profile,'--golden',$Merged.golden_manifest))
        Invoke-Monitor ($Link + @('verify-deployment','--profile',$Target.Profile,'--golden',$Merged.golden_manifest))
        $Test = "CAL-$($Target.Name)-$($Board.Id)-$SESSION"
        for ($Index=1; $Index -le $RunsPerBoard; $Index++) {
            $Attempt = '{0}-{1:D3}' -f $Test,$Index
            Invoke-Monitor ($Link + $Serial + @('run','--profile',$Target.Profile,
                '--golden',$Merged.golden_manifest,'--telemetry','--pc-artifacts','full',
                '--test-id',$Test,'--attempt-id',$Attempt))
            Invoke-Monitor ($Link + @('collect','--test-id',$Test,'--attempt-id',$Attempt,
                '--artifact-set','calibration','--verify-hashes'))
            $Samples[$Target.Name] += @('--run-dir',"$($Board.Id)=$OUT\$Test\$Attempt")
        }
    }
    $CalibrationArgs = @('calibrate',$Target.Name,'--profile',$Target.Profile,
        '--board-id','BOARD-A','--golden',$Goldens[$Target.Name],
        '--runs',[string](2*$RunsPerBoard),'--baseline-id',"$($Target.Name)-$SESSION")
    if ($RunsPerBoard -eq 1) { $CalibrationArgs += @('--min-accepted','2') }
    Invoke-Monitor ($CalibrationArgs + $Samples[$Target.Name])
}
```

若某个命令失败，示例立即停止；从其 JSON 记录 test_id、attempt_id、result 路径，按下表取证。修复后使用新的 session/attempt，不覆盖原记录。

功能闭环的两条样本只生成 draft，不应批准为生产 baseline。生产 cohort 使用默认策略：至少两板、至少20个接受样本，建议每板30次；`runs_per_board` 是采样计划数量而非隐藏的 CLI 拒绝条件。拒绝重复 run_id，即使操作者给它写了不同 board_id。

## Calibration 的真实检查

calibrate 是离线统计，不执行 workload，也不寻找 Vmin。它要求：PC verdict=PASS/exit=0，run manifest 为 golden-reference，profile/target/golden 指纹与当前输入一致；完整 native summary 的实际校验次数吻合 batch/frame 数量，无校验失败；持续时间至少达到配置的90%，CPU至少2个batch；required遥测快照完整，长测试的时间戳、间隔及覆盖跨度符合采样周期；没有已报告的 throttling，温度在允许范围内。

默认通过样本的吞吐下限为 observed_min×0.95，延迟上限为 observed_max×1.10。策略中的 maximum_cv=10% 现在确实执行：超过则拒绝整个不稳定 cohort，不通过放宽性能阈值掩盖。检查 proposed-baseline.json 的 accepted_per_board、rejected_runs、分布及阈值后再人工 approve。

## 重点取证，不再每次全量回收

PC 每次始终保留 `result.json`、`run-manifest.json`。排查 UART 时运行前加 `--pc-artifacts full`，这样才有 PC `serial.raw`/`events.jsonl`；事后 collect 不能重建设备从未记录的 PC 原始串口流。

| 情况 | collect 选项 | 设备重点文件（spool 下） |
|---|---|---|
| 正常运行、只需核对结论 | `--artifact-set minimal` | `workload-summary.json`、`final.json` |
| 性能资格化/离线calibrate | `--artifact-set calibration` | 上述文件，加 `telemetry.jsonl`、可用的 `telemetry-agent.log` |
| checksum/golden不一致、workload退出/阻塞 | `--artifact-set failure` | 上述文件及 workload stdout/stderr/diagnostics、events、relay、可用遥测 |
| UART CRC/丢帧/FINAL超时 | `--artifact-set protocol` | summary/final、`events.jsonl`、`relay.log`，同时保留PC serial.raw |
| 断电、hash manifest未生成、首次未知故障 | `--artifact-set full` | 完整attempt；缺hash时先不加 `--verify-hashes`，明确标为不完整证据 |
| golden生成 | live golden自动全量回收 | CPU golden事件；GPU额外保留 `gpu-golden.rgba`，不能用minimal替代 |

子集必须指定 `--attempt-id`，始终下载 artifact-hashes.json 并验证所选文件，collection.json 明确记录 omitted_files 和 selected-files-only。某文件未生成时，不等于“该检查通过”。calibration 缺summary/telemetry直接拒绝；故障结束缺summary可以取minimal/failure。没有完整hash manifest则用full诊断。

子集禁止 `--remove-remote-after-verify`，避免把没有回收的证据删掉；只有完整回收并验证全部文件后才允许显式清理。普通 PASS 无需再额外复制整棵测试目录。

## AVS 判定与负向验收

- `verify_mode=none` 只能检出 workload主动错误、退出和失活，不能证明输出正确；它的 PASS 不能标作正确性 Vmin。
- checksum/golden错误是 DUT 原因；缺校验次数、部署不一致、协议证据不完整是基础设施/配置问题，不直接判定芯片电压极限。
- 正式GPU采用 exact golden。较宽像素阈值只能另作审计，不能覆盖 exact失败；两板额定电压 golden不一致时先排查配置、二进制、shader、驱动及确定性。
- 性能阈值是额外约束，可能在更高电压首先触发，但不保证每块板都如此；明确分别报告 correctness、performance、liveness和基础设施原因。
- 回归至少覆盖：错误checksum导致首个verify立即可见且后续无summary仍保留；成功日志洪流不堵UART；verify_count=0/缺失/不匹配拒绝PASS；旧binary/漏golden启动前拦截；两板错误指纹/非PASS/遥测缺口/CV过大拒绝calibrate。仓库自动测试覆盖这些可控场景，实机仍需按两板分别记录实际结果。
- 600秒 thermal等长profile运行时显式增大 `--overall-timeout`（例如760秒），并先在额定电压确认单batch/frame用时；不要通过无限增大heartbeat窗口掩盖挂死。

完整的 probe/pair、telemetry独立测试、baseline生命周期、模拟及报告接口，继续按 [PACKAGE_AND_DEVICE_TEST_PLAN.md](PACKAGE_AND_DEVICE_TEST_PLAN.md) 逐项验收。其旧验收记录是历史记录，不代表本次改动的硬件验证。
