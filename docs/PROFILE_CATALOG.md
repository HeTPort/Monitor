# Workload profile catalog

These profiles target the CPU backends and Vulkan offscreen shader paths implemented by the workload source. The additional configurations have host-side schema and reference validation; they are starting points for board testing, not claims of Kirin9030 hardware qualification. Profile YAML files are under `config/profiles/` and point to the corresponding workload JSON under `config/workloads/`.

All live configurations use `verify_interval: 1`, `success_log_interval: 60`, and `summary_only: false`. A correctness profile checks every completed batch/frame and emits the first successful verification, then every 60 additional successes. Failures are reported immediately. Ordinary load-only profiles use `verify_mode: none`; a PASS on these does not prove computation/image correctness. The old `checksum_interval` spelling is accepted with a deprecation warning and means actual verification cadence, not logging cadence. Do not specify both names.

## Kirin9030 CPU

| Profile | Work performed | Duration / warmup / timeout (s) | Verification and use |
| --- | --- | --- | --- |
| `cpu_smoke_kirin9030` | Small mixed workload, one thread | 10 / 1 / 20 | None; deployment/transport smoke |
| `cpu_stress_kirin9030` | Mixed, four threads | 60 / 5 / 75 | None; load-only reference |
| `cpu_qualification_kirin9030` | Mixed, four threads, 256 KiB per thread | 60 / 5 / 75 | Checksum; normal sustained correctness/baseline candidate |
| `cpu_integer_qualification_kirin9030` | Integer backend, 100000 iterations, 64 KiB per thread | 60 / 5 / 75 | Checksum; integer coverage |
| `cpu_floating_point_qualification_kirin9030` | Floating-point backend, 100000 iterations, 64 KiB per thread | 60 / 5 / 75 | Checksum; floating-point coverage |
| `cpu_matrix_qualification_kirin9030` | Matrix backend, 20000 iterations, 96 KiB per thread | 60 / 5 / 75 | Checksum; matrix coverage |
| `cpu_memory_qualification_kirin9030` | Memory backend, 500000 iterations, 1024 KiB per thread | 60 / 5 / 75 | Checksum; memory-path coverage, not an exhaustive DRAM test |
| `cpu_burst_qualification_kirin9030` | Mixed, one active second per two-second period | 120 / 5 / 160 | Checksum during active batches; load transitions |
| `cpu_thermal_qualification_kirin9030` | Mixed, 200000 iterations, 512 KiB per thread | 600 / 10 / 660 | Checksum; sustained heating/soak |
| `cpu_stress_extreme_qualification_kirin9030` | Mixed, 500000 iterations, 2048 KiB per thread | 120 / 10 / 180 | Checksum; heavier batch/working-set load |
| `cpu_idle_control_kirin9030` | Null backend with duty cycle zero | 30 / 0 / 60 | None; heartbeat/telemetry control with zero measured batches; never a golden or performance baseline input |

All additional active CPU configurations use four workload threads with no hard-coded affinity, core mask, frequency or voltage. The `stress_extreme` load comes from explicit iteration/working-set values; its name is not a guarantee of maximum power. Burst has deliberately variable throughput, so do not use the default steady-performance calibration policy for it. A thermal run may intentionally leave the calibration temperature window; correctness/soak results and baseline eligibility remain separate.

## Kirin9030 GPU

All GPU profiles use Vulkan, offscreen rendering, RGBA8 and one sample. ALU and SFU are fragment-shader paths; the workload's built-in profile defaults select `compute`, so the Monitor JSON explicitly overrides them to the implemented `offscreen` mode.

| Profile | Render/load configuration | Duration / warmup / timeout (s) | Verification and use |
| --- | --- | --- | --- |
| `gpu_smoke_kirin9030` | Mixed, 640x360, 32 iterations | 10 / 1 / 20 | None; deployment/transport smoke |
| `gpu_stress_kirin9030` | Mixed, 1280x720, 192 iterations | 60 / 5 / 90 | None; load-only reference |
| `gpu_qualification_kirin9030` | Mixed, 1280x720, 192 iterations | 60 / 5 / 90 | Strict golden image; normal correctness/baseline candidate |
| `gpu_alu_qualification_kirin9030` | ALU, 1280x720, 512 iterations | 60 / 5 / 90 | Strict golden image; arithmetic shader coverage |
| `gpu_sfu_qualification_kirin9030` | SFU sin/cos, 1280x720, 512 iterations | 60 / 5 / 90 | Strict golden image; transcendental shader coverage |
| `gpu_texture_qualification_kirin9030` | Texture, 1280x720, 128 iterations, four samples per inner iteration | 60 / 5 / 90 | Strict golden image; texture sampling coverage |
| `gpu_fill_qualification_kirin9030` | Fill, 1920x1080, simple output shader | 60 / 5 / 90 | Strict golden image; fill/render-target coverage |
| `gpu_load_light_kirin9030` | Mixed, 1280x720, 128 iterations, sampling multiplier 2 | 60 / 5 / 95 | None; continuous lighter graphics load |
| `gpu_load_mid_kirin9030` | Mixed, 1920x1080, 192 iterations, sampling multiplier 4 | 60 / 5 / 95 | None; continuous medium graphics load |
| `gpu_load_heavy_kirin9030` | Mixed, 2560x1440, 384 iterations, sampling multiplier 6 | 60 / 5 / 120 | None; continuous heavier graphics load |
| `gpu_thermal_kirin9030` | Mixed, 1920x1080, 512 iterations | 600 / 10 / 700 | None; long continuous heating/load |
| `gpu_stress_extreme_kirin9030` | Mixed, 3840x2160, 1024 iterations, sampling multiplier 8, 4096x4096 texture | 120 / 10 / 240 | None; high-resolution intensive load; establish nominal-voltage runtime first |

The Vulkan backend creates one texture. `texture_count` changes repeated sampling and deterministic texture data; it does not allocate that many independent textures. The fill shader ignores `iterations`; resolution is its principal load control. `game_light/mid/heavy` in the workload JSON are names of built-in defaults, not actual game benchmarks. The Monitor light/mid/heavy profiles fix `duty_cycle: 1` and define continuous load using resolution, iteration count and sampling count.

Vulkan `compute`, non-RGBA8 formats, multisampling, idle, burst and fractional duty-cycle scheduling are not implemented by this backend and are not advertised here. Monitor rejects incompatible modes and duty cycles before deployment. The GPU null backend has burst scheduling, but that does not establish real GPU coverage. Pixel-diff verification remains outside Monitor's formal strict-golden contract.

## Choosing and running profiles

Start at nominal voltage with smoke, then the existing mixed qualification profile. Add the four CPU backend profiles and the four GPU shader profiles to examine coverage. Use burst/thermal/extreme profiles for the specific load condition they describe, after checking ordinary operation on the board. Workload timeouts remain finite and can reveal that a proposed batch/frame load is unsuitable for a given device.

For each correctness profile, independently follow `deploy -> verify-deployment -> golden on both known-good boards -> merge/validate golden -> deploy --golden on each board -> verify-deployment --golden -> run --golden --telemetry`. The complete commands and evidence collection are in [the two-board test plan](PACKAGE_AND_DEVICE_TEST_PLAN.md). GPU golden paths are unique per profile; never copy a mixed golden into an ALU/SFU/texture/fill profile. A change in binary, shader or effective configuration requires a new golden and new performance samples.

For load-only profiles, use `deploy -> verify-deployment -> run`; do not present their error-only PASS as the correctness Vmin. If strict verification is needed under a new load configuration, create a separately named correctness profile with `golden-image`, a unique golden path and its own nominal-voltage golden instead of reusing a different configuration's golden.

Calibration takes complete, correct, stable sustained samples with required telemetry; it does not test the whole catalog automatically. Calibrate each steady correctness profile separately, with two board IDs and the configured accepted-sample requirement. Idle, intentionally bursting throughput and thermal-policy violations are not substitutes for stable baseline samples.

## Existing Kirin9020 profiles

`cpu_mixed_big4` and `gpu_vulkan_mixed` remain Kirin9020 profiles with their existing scheduler metadata. They also use the new verification/logging fields. Do not substitute them for Kirin9030 profiles or infer that their core-affinity/OPP metadata applies to another platform. The older `config/workload_profiles.yaml` is a legacy registry; the profile YAML directory is the catalog used by the main CLI.
