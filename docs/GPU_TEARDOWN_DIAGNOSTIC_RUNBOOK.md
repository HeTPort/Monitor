# GPU 正常结束后 SIGSEGV 的诊断与复测手册

本文根据 `MonitorTest/vmin_judge_0910/TEST_RECORD.txt` 和随附证据整理。目标是定位 GPU workload 已输出 PASS summary、随后却以 139 退出的问题。本文是待执行的诊断计划，不把尚未执行的测试写成结论。

## 当前结论

- CPU stress、CPU smoke、telemetry、双板 CPU golden/calibration/baseline 闭环均已通过。当前问题应先收敛在 GPU 原生 workload 生命周期，不应回退已经通过的 CPU 路径。
- `MC-GPU-0909A-001` 已输出 contract v2 summary：`result=PASS`、`exit_code=0`、`frame_count=603`，但设备 agent 等到的真实进程状态为 139，stderr 为 `Signal 11 (core dumped)`。Monitor 因此给出 `INFRA_ERROR`，并报告 `WORKLOAD_EXIT_MISMATCH`、`AGENT_EXIT_MISMATCH` 和 `WORKLOAD_EXIT_NONZERO`。这个判定是正确的，不能因为 summary 为 PASS 而放行。
- 该运行没有 overall/heartbeat/final timeout，`terminal_summary_seen=true`、`agent_final_seen=true`，设备证据和哈希完整。因此目前没有证据支持“Monitor 在 summary 后主动杀进程”或“串口超时导致 139”。
- 0908 的相同 `gpu_stress_kirin9030` profile 曾正常退出 0；问题更像是构建产物或近期 GPU workload 变更引入的回归，而不是 profile 天生不可运行。
- GPU workload 成功路径在 `gpuworkload/src/runner.cpp` 中显式调用 `backend->Destroy()`，之后才输出 summary；backend 智能指针在函数离开时还会进入析构。`VulkanGraphicsBackend` 析构函数也调用 `Destroy()`。因此“显式销毁完成后，在自动对象析构阶段崩溃”是重点方向，但在拿到 native crash stack 前不能直接认定为二次销毁。
- 近期 contract v2 修改扩展了 `WorkloadConfig`、`SummaryData` 和 Logger 数据/输出。若设备二进制采用增量构建，旧对象文件与新头文件混用可能造成 ABI/对象布局不一致；即使 clean build 后仍复现，也可能是此前已存在的越界写或 use-after-free 直到退出阶段才被检测到。
- 当前 PC 自动化 GPU runtime 测试使用 null backend，不能覆盖 OpenHarmony 真机 Vulkan 创建、执行、Destroy 和进程退出，所以现有测试无法发现此类回归。

## 怀疑方向和判别信号

| 优先级 | 怀疑方向 | 支持证据 | 如何证伪或确认 |
| --- | --- | --- | --- |
| P0 | 增量构建产生新旧对象混用，导致结构体布局/ABI 不一致 | contract 修改涉及多个跨编译单元结构；0908 通过、0909 新产物失败 | 同一源码做完全 clean rebuild；新产物通过而旧产物稳定失败，即强支持此方向 |
| P0 | GPU 执行阶段破坏堆内存，退出/析构时才触发 | summary 已输出，真实退出为 SIGSEGV | native stack 落在 `free`、allocator、`std::string`/容器析构，或只有长压触发 |
| P1 | Vulkan backend Destroy/析构重复进入或清理顺序错误 | 成功路径显式 Destroy，析构又可调用 Destroy；崩溃发生在 summary 之后 | stack 落在 `VulkanGraphicsBackend::~VulkanGraphicsBackend`、`Destroy` 或 Vulkan destroy API；生命周期计数显示同一实例进入两次 |
| P1 | Vulkan 驱动/资源释放在长负载后失败 | GPU 执行本身完成，错误位于退出窗口 | smoke 全过而 stress 稳定失败，且 faultlog/hilog/dmesg 指向 Vulkan 驱动或 GPU fault |
| P2 | Monitor agent/FIFO/shell 环境特有交互 | 目前 agent 正确回报了 139，尚无直接支持 | 原生直跑全部通过，而相同二进制和配置仅经 Monitor 启动时失败 |

在获得下面测试证据前，不建议先删掉某次 `Destroy()` 或让 agent 忽略 139。前者可能掩盖真正的堆破坏，后者会把进程崩溃误报为 PASS。

## 测试前提：必须固定输入

明天开始测试前，先从 GPU workload 源码做一次完全 clean rebuild，不复用旧的 `.o`、CMake cache 或旧输出目录。当前仓库不包含真机 GPU 的完整交叉编译入口，所以本文不虚构构建命令；请把实验室实际使用的 clean/build 命令、工具链版本、源码 commit 和最终二进制 SHA-256 原样保存到测试目录。

若 0910 的失败二进制仍在设备或发布目录中，应在覆盖前先保存它及其 SHA-256。最有价值的 A/B 是“0910 原二进制直跑 stress 至少一次”与“同源码完全 clean build 后的二进制按测试 1 执行”；若旧产物已经丢失，在结论中明确写明，不能声称已完成旧/新产物对照。

然后使用同一发布包完成部署并核验。下面的路径与 0910 记录一致，按现场实际值修改设备 ID 和发布目录：

```powershell
$MON = 'D:\h60106817\release\vmin_judge.exe'
$DEVICE = '0123456789ABCDEF'
$PC_SERIAL = 'COM8'
$DEVICE_UART = '/dev/ttyHW0'
$SESSION = Get-Date -Format yyyyMMdd-HHmmss
$LOCAL = "D:\AVS-results\GPU-TEARDOWN-$SESSION"

New-Item -ItemType Directory -Force $LOCAL | Out-Null
Start-Transcript "$LOCAL\pc-transcript.txt"

& $MON --transport hdc --device $DEVICE deploy --profile gpu_smoke_kirin9030
if ($LASTEXITCODE -ne 0) { throw 'gpu smoke deploy failed' }
& $MON --transport hdc --device $DEVICE deploy --profile gpu_stress_kirin9030
if ($LASTEXITCODE -ne 0) { throw 'gpu stress deploy failed' }
& $MON --transport hdc --device $DEVICE verify-deployment --profile gpu_smoke_kirin9030
if ($LASTEXITCODE -ne 0) { throw 'gpu smoke deployment verification failed' }
& $MON --transport hdc --device $DEVICE verify-deployment --profile gpu_stress_kirin9030
if ($LASTEXITCODE -ne 0) { throw 'gpu stress deployment verification failed' }
```

不要在测试 1 和测试 2 之间替换 binary、config 或 shader。若必须替换，应开始新的 session，不能把两组结果放在同一比较组内。

## 测试 1：原生进程生命周期矩阵

这是明天第一优先级测试，用于把 GPU workload 自身问题与 Monitor 包装层问题分开。矩阵固定为：

| 子项 | 命令内容 | 次数 | 通过条件 |
| --- | --- | ---: | --- |
| T1-A control | `gpu-avs-workload --capabilities` | 3 | 三次真实退出码均为 0，输出均为合法 capabilities JSON，无 crash 文件 |
| T1-B smoke | 使用已部署的 `gpu_smoke_kirin9030.json` 原生直跑 | 3 | 每次恰好一个 summary，summary PASS/0，真实进程退出 0，stderr 无异常 |
| T1-C stress | 使用已部署的 `gpu_stress_kirin9030.json` 原生直跑 | 3 | 每次恰好一个 summary，summary PASS/0，真实进程退出 0，stderr 无异常 |

必须记录远端 shell 的真实 `$?`。HDC 自身返回 0 不等于设备进程返回 0；下面的命令把设备退出码先写入 `.exit` 文件，再由 PC 拉回。

```powershell
$REMOTE = "/data/local/tmp/avs/tests/GPU-TEARDOWN-T1-$SESSION"

& hdc -t $DEVICE shell "mkdir -p $REMOTE"
if ($LASTEXITCODE -ne 0) { throw 'cannot create remote evidence directory' }

$HashPaths = @(
    '/data/local/tmp/avs/bin/gpu-avs-workload',
    '/data/local/tmp/avs/configs/gpu_smoke_kirin9030.json',
    '/data/local/tmp/avs/configs/gpu_stress_kirin9030.json',
    '/data/local/tmp/avs/shaders/vulkan/fullscreen.vert.spv',
    '/data/local/tmp/avs/shaders/vulkan/workload.frag.spv'
)
foreach ($Path in $HashPaths) {
    & hdc -t $DEVICE shell "sha256sum $Path" |
        Add-Content "$LOCAL\input-sha256.txt"
}

& hdc -t $DEVICE shell '/data/local/tmp/avs/bin/gpu-avs-workload --version' |
    Set-Content "$LOCAL\version.txt"
& hdc -t $DEVICE shell '/data/local/tmp/avs/bin/gpu-avs-workload --capabilities' |
    Set-Content "$LOCAL\capabilities.json"
& hdc -t $DEVICE shell 'ls -lR /data/log/faultlog 2>&1' |
    Set-Content "$LOCAL\faultlog-before.txt"
& hdc -t $DEVICE shell 'hilog -d 2>&1' |
    Set-Content "$LOCAL\hilog-before.txt"

function Invoke-DeviceCase {
    param(
        [string]$Name,
        [string]$Arguments,
        [int]$Repeat
    )

    for ($Index = 1; $Index -le $Repeat; $Index++) {
        $Tag = "$Name-$Index"
        $Command = "cd /data/local/tmp/avs; " +
            "./bin/gpu-avs-workload $Arguments " +
            "> $REMOTE/$Tag.stdout 2> $REMOTE/$Tag.stderr; " +
            "rc=`$?; echo `$rc > $REMOTE/$Tag.exit; sync"

        & hdc -t $DEVICE shell $Command
        if ($LASTEXITCODE -ne 0) { throw "HDC failed while launching $Tag" }
    }
}

Invoke-DeviceCase 'control' '--capabilities' 3
Invoke-DeviceCase 'smoke' '--config /data/local/tmp/avs/configs/gpu_smoke_kirin9030.json' 3
Invoke-DeviceCase 'stress' '--config /data/local/tmp/avs/configs/gpu_stress_kirin9030.json' 3

& hdc -t $DEVICE shell 'ls -lR /data/log/faultlog 2>&1' |
    Set-Content "$LOCAL\faultlog-after.txt"
& hdc -t $DEVICE shell 'hilog -d 2>&1' |
    Set-Content "$LOCAL\hilog-after.txt"
& hdc -t $DEVICE shell 'dmesg 2>&1' |
    Set-Content "$LOCAL\dmesg-after.txt"
& hdc -t $DEVICE file recv $REMOTE "$LOCAL\device"
if ($LASTEXITCODE -ne 0) { throw 'failed to pull native test evidence' }
```

拉回后对 smoke/stress 做机械检查，避免只看 summary 忽略真实退出码：

```powershell
$Failures = @()
foreach ($ExitFile in Get-ChildItem "$LOCAL\device" -Recurse -Filter '*.exit') {
    $Tag = $ExitFile.BaseName
    $ExitCode = (Get-Content $ExitFile.FullName -Raw).Trim()
    $Stdout = Join-Path $ExitFile.DirectoryName "$Tag.stdout"
    $Stderr = Join-Path $ExitFile.DirectoryName "$Tag.stderr"

    if ($ExitCode -ne '0') {
        $Failures += "$Tag real exit=$ExitCode"
    }
    if ($Tag -notlike 'control-*') {
        $JsonLines = @(Get-Content $Stdout | ForEach-Object {
            try { $_ | ConvertFrom-Json -ErrorAction Stop } catch { $null }
        })
        $Summaries = @($JsonLines | Where-Object { $_.type -eq 'summary' })
        if ($Summaries.Count -ne 1) {
            $Failures += "$Tag summary count=$($Summaries.Count)"
        } elseif ($Summaries[0].result -ne 'PASS' -or $Summaries[0].exit_code -ne 0) {
            $Failures += "$Tag summary is not PASS/0"
        }
    }
    if ((Get-Item $Stderr).Length -ne 0) {
        $Failures += "$Tag stderr is not empty"
    }
}

if ($Failures.Count -ne 0) {
    $Failures | Set-Content "$LOCAL\native-validation-failures.txt"
    $Failures
} else {
    'T1 native lifecycle matrix PASS' |
        Set-Content "$LOCAL\native-validation-pass.txt"
}
```

比较 `faultlog-before.txt` 和 `faultlog-after.txt`。把新增的 `cppcrash-*`、tombstone 或同类 native crash 文件逐个拉回，不只保留目录清单：

```powershell
$CRASH = '/data/log/faultlog/<新增的崩溃文件名>'
& hdc -t $DEVICE file recv $CRASH "$LOCAL\crash"
```

若 `/data/log/faultlog` 不存在或无权限，仍需保存命令原始输出，并向系统维护者确认该版本的 native faultlogger 目录；不要用“未看到文件”代替 crash stack 结论。

## 测试 2：同一输入经 Monitor 包装复现

测试 1 后不重新 deploy，使用相同设备文件各运行 smoke/stress 三次。这里统一使用 `--pc-artifacts full` 和完整 collect，因为目标是诊断而不是节省存储。

```powershell
function Invoke-MonitorGpuCase {
    param(
        [string]$Name,
        [string]$Profile
    )

    $TestId = "GPU-$Name-$SESSION"
    for ($Index = 1; $Index -le 3; $Index++) {
        $AttemptId = '{0}-{1:D3}' -f $TestId, $Index
        $RunOutput = & $MON --transport hdc --device $DEVICE `
            --pc-serial $PC_SERIAL --device-uart $DEVICE_UART --json `
            run --profile $Profile --test-id $TestId --attempt-id $AttemptId `
            --pc-artifacts full 2>&1
        $RunExit = $LASTEXITCODE
        $RunOutput | Set-Content "$LOCAL\$AttemptId-monitor-run.txt"
        $RunExit | Set-Content "$LOCAL\$AttemptId-monitor-exit.txt"

        $CollectOutput = & $MON --transport hdc --device $DEVICE --json `
            collect --test-id $TestId --attempt-id $AttemptId `
            --artifact-set full --verify-hashes 2>&1
        $CollectExit = $LASTEXITCODE
        $CollectOutput | Set-Content "$LOCAL\$AttemptId-monitor-collect.txt"
        $CollectExit | Set-Content "$LOCAL\$AttemptId-collect-exit.txt"
    }
}

Invoke-MonitorGpuCase 'SMOKE' 'gpu_smoke_kirin9030'
Invoke-MonitorGpuCase 'STRESS' 'gpu_stress_kirin9030'
Stop-Transcript
```

每次都必须同时核对 `result.json` 中的 `workload_summary.exit_code`、`workload_exit_code`、`infrastructure_reasons` 和 verdict。判定规则保持不变：summary PASS/0 但真实进程为 139，仍是失败。

## 必须回收的结果

测试完成后提交一个以 session 命名的目录，至少包含：

- PC transcript、每条命令和 PC/HDC 返回码；GPU workload 源码 commit、完整 clean/build 命令和工具链版本。
- GPU binary、smoke/stress config、两个 SPIR-V shader 的 SHA-256，以及 workload `--version`、`--capabilities` 原始输出。
- T1 九次原生运行的 `.stdout`、`.stderr`、`.exit`，不能只摘录 PASS summary。
- faultlog 前后目录清单、所有新增 native crash 文件、`hilog-after.txt` 和 `dmesg-after.txt`。
- T2 六次 Monitor 的 `result.json`、`run-manifest.json`、`effective-profile.json`；PC `serial.raw`、`events.jsonl`；设备 `final.json`、`workload-summary.json`、workload stdout/stderr、agent/relay/diagnostics 日志、`artifact-hashes.json` 及 collect 回执。
- 一张结果表，逐行列出 test/attempt、binary SHA-256、profile、summary result/exit、真实进程 exit、Monitor verdict、是否产生 crash stack、stack 顶部五个符号。

不要覆盖 0910 的证据目录；新测试必须使用新的 test/attempt ID。远端证据在哈希核验和备份前不要删除。

## 根据结果选择修复

| 观测结果 | 结论倾向 | 下一步修改 |
| --- | --- | --- |
| clean build 后所有 T1/T2 均通过，而旧二进制可复现 | 增量构建/ABI 混用 | 恢复并固化 GPU clean-build 入口；CI 禁止复用不兼容对象；在 `--version`/capabilities 写入源码 commit、工具链和 build ID |
| control 也崩溃 | 与 Vulkan workload 无关的通用启动/退出、ABI 或运行库问题 | 先查 loader、全局/static 析构和混合运行库，不修改 Vulkan资源顺序 |
| control 通过，smoke 和 stress 原生直跑均崩溃 | Vulkan 公共生命周期/对象析构问题 | 依据 native stack 检查 Destroy 幂等性、成员析构顺序和资源所有权；增加 exactly-once 生命周期状态与断言 |
| smoke 连续通过，stress 原生直跑失败 | 负载时长相关堆破坏、资源泄漏或驱动问题 | 缩短/二分 duration 和帧数，开启可用 sanitizer/allocator diagnostics，结合 GPU fault/hilog 定位首个异常 |
| T1 全过，只有 T2 失败 | agent/FIFO/shell/UART 环境交互 | 对比 T1/T2 argv、cwd、环境变量、fd、信号和 agent 日志；增加 agent 启动的原生退出契约测试 |
| stack 顶部在 `free`、allocator、`std::string`/容器析构 | 越界写、double free、ABI 布局不一致 | 优先做 ASan/HWASan 或平台可用堆诊断；不要只在析构处加空指针判断 |
| stack 明确落在 `VulkanGraphicsBackend::Destroy` 或 Vulkan destroy API | Destroy 重入或 Vulkan 资源释放顺序 | 以 stack 对应资源为入口修复；让单一 RAII owner 负责销毁，并以状态机保证每个 handle 最多销毁一次 |

若修改生命周期代码，至少新增以下自动测试后再合入：

1. fake backend 生命周期测试：正常成功、Init 失败、CreateResources 失败、运行中错误、验证失败、timeout/cancel 六条路径，每个 backend 实例恰好清理一次。
2. 进程契约测试：已经输出 summary 后发生 SIGSEGV/非零退出时，agent 必须保留真实进程状态，Monitor 必须拒绝 PASS；不得把 summary exit 覆盖进程 exit。
3. OpenHarmony Vulkan 真机门禁：clean build 后 smoke 3 次、stress 3 次，要求 summary PASS/0 与真实退出 0 同时成立且无新增 native crash。
4. 构建可追溯测试：发布包记录 GPU 源码 commit、工具链/build ID，并核验二进制、配置和 shader 哈希；无法证明 clean build 的产物不能进入正式资格化。

## 本轮完成标准

本问题只有在以下条件同时满足时才能关闭：根因由 native stack 或可重复 A/B 实验支持；修复前能稳定失败、修复后 clean build 的 T1/T2 全部通过；新增自动生命周期/退出契约测试通过；真机 Vulkan smoke/stress 连续三次无 SIGSEGV；Monitor 仍严格拒绝 summary 与真实退出码不一致的运行。
