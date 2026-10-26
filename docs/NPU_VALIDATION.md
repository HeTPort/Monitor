# NPU software validation and hardware acceptance

Validation date: 2026-10-26 (Asia/Shanghai).

## Baselines and isolation

| Repository | Baseline | Development checkout / branch |
|---|---|---|
| Monitor | `74bc567` (`codex/verify-contract-profiles`) | `D:\Monitor-main\npu-monitor`, `codex/npu-monitor-integration` |
| Workload | `ccf431f` (main, four NPU models/delegate backend) | `D:\Monitor-main\npu-workload`, `codex/npu-monitor-protocol` |

Both checkouts are independent local Git clones with new branches. Original main repositories, user planning files, and the September worktrees were not overwritten, reset, merged or pushed. The CPU/GPU verification branch was inspected for compatibility; only its small verification-scheduling header was reused in the NPU project. No CPU/GPU workload source changes are included.

## Executed checks

| Check | Result / scope |
|---|---|
| Original Monitor baseline, Windows | 148 tests pass, 9 POSIX tests skipped; PyYAML installed in workspace test dependencies |
| Original Monitor baseline, WSL/Linux | 148 tests pass, no skips |
| Integrated Monitor regression, WSL/Linux | 172 tests pass, including native NPU -> shell agent -> framed UART -> policy tests |
| Integrated Monitor regression, Windows | 172 tests pass, 12 POSIX-dependent tests skipped |
| NPU protocol executable tests, Linux | 11 tests pass, including four fake-delegate profiles |
| NPU protocol executable tests, Windows | 11 tests pass, including four fake-delegate profiles |
| Existing NPU Windows smoke suite | 13 cases pass, including expected error exits |
| Native manifest/tensor contract executable | All five registered manifests pass (four real model profiles plus framework fixture) |
| Builds | Linux direct g++ and Windows build.ps1/g++, C++17, -Wall -Wextra -Wpedantic; no compiler warnings observed |
| Static checks | Python compileall, PowerShell build script parser, Git whitespace checks |

The environment has no usable CMake command; native g++ was used for Linux and the existing PowerShell fallback for Windows. Cross-compilation/package production was not claimed. Verification logs are under `docs/validation/`.

## Covered acceptance boundaries

- Existing CPU/GPU configuration, CLI, deployment/preflight, failure latch, golden/calibration, transport, artifact, UART and reporting tests remain passing.
- Twelve NPU model/use-case configurations are validated. Warm-up remains a count; malformed types, aliases, finite/range errors and unsafe path delimiters are rejected.
- Complete manifest/model/input asset resolution, exact registered tensor descriptors, relative layout, deterministic effective config and declared SHA-256 checks are exercised for all four models. Host objects cannot masquerade as AArch64 device assets.
- Capabilities are backend-free; contract version, modes, targets, supported models/backends and successful-log bounds are checked. Workload-generated actual verification counts cannot be inferred from successful-event volume.
- Warm-up/golden generation are excluded from measurement/check counters. Disabled/sampled success logs retain all actual checks. An incorrect golden fails immediately even with success logging disabled; partial failure counters remain actual.
- NPU inference events remain local, first verify failure survives a missing summary, long native partitions remain local, and essential summary evidence is split within the existing 512-byte UART bounds. Sequence/CRC/session/FINAL behavior remains shared.
- Framed-event and raw-UART NPU replay produce the same verdict and reject missing actual verification counts.
- Explicit known-good approval, native/PC/FINAL identity, source hashes, native model identity, real backend and vendor-evidence identity are required for formal qualification. Synthetic accepted/rejected cohort tests cover NPU throughput/host-latency calibration and draft baseline creation.
- Unknown device timing remains unavailable. Run consistency and approved external correctness are separately reported. Unknown placement is never inferred from `fallback_used=false`.

## Required device acceptance (not performed)

1. Cross-build the NPU executable and relay with the actual HarmonyOS/Android SDK; stage the models and actual AArch64 runtime/delegate libraries and managed dependencies.
2. Verify dynamic-loader ABI/permissions, the actual HDC device and UART, and all installed/managed asset hashes.
3. Validate real platform NPU temperature/frequency/utilization/power interfaces and required/optional policy. Templates deliberately contain no guessed NPU sysfs paths. Populate required telemetry before collecting production calibration samples.
4. Run KWS01 through `validate -> probe -> deploy -> verify-deployment -> run -> collect`, inspect vendor execution diagnostics, then repeat IC01/AD01/SWW01.
5. Acquire and acknowledge the golden at independently trusted physical operating conditions. Attach vendor diagnostics proving full placement for the exact asset identity. Hashes bind an operator attestation; they do not independently establish that the diagnostic proves full placement.
6. Execute negative device tests: bad golden, missing/changed libraries, wrong deployed hash, initialization/inference stall, hard process cleanup and truncated/mismatched UART sessions. Software fixtures do not certify vendor runtime behavior.
7. Perform the production multi-board sustained cohort and temperature/operating-point matrix, approve the resulting draft baseline explicitly, then verify repeated production runs/replay/reports.

Null/fake-runtime passes are framework/protocol evidence only. No board-level NPU execution, full placement, AVS correctness, power savings, thermal qualification or completed production package is claimed by this change.

Paired workload delivery commit: `855102877c5374c949935621007f6538a47106b2` on `codex/npu-monitor-protocol` (verification contract 2, NPU version 0.4.0). The Monitor commit containing this record is the matching integration revision.
