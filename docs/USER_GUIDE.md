# Vmin Monitor User Guide and Public API Reference

Status: Implemented v2 interface; hardware validation status is listed below

Audience: Qualification engineers, batch-test operators, automation authors, and integration developers

Design reference: [DEVELOPMENT_AND_DESIGN.md](DEVELOPMENT_AND_DESIGN.md)

## 1. Purpose of this guide

This guide defines how users and automation interact with the refactored Vmin Monitor. It documents each public interface by its purpose, origin, rationale, usage, inputs, outputs, and failure behavior.

The v2 PC CLI, schemas, qualification services, baseline registry, policy engine, deployment layer, and fixed device-agent script are implemented. `--version` reports the supported schema majors, and `validate --package --offline` checks packaged resources and configuration without a board.

### 1.1 Current validation boundary

- Offline unit, integration, protocol, simulation, and frozen-path tests are implemented and passing.
- The checked-in device agent is a fixed POSIX Shell implementation. It does not require Python or `jq`; the PC resolves the run manifest and passes a safe argument vector to the agent.
- Single-/dual-framework interface equivalence, actual sysfs/debugfs permissions, workload binary behavior, UART capacity, and CPU/GPU limit quality still require hardware-in-the-loop qualification on the office UDP boards.
- `vmin_judge.spec` and `scripts/build.ps1` implement packaging. A release executable must still be built and smoke-tested in an environment containing PyInstaller, PyYAML, and pyserial.

The APIs in scope are:

- Packaged PC command-line interface.
- Device-agent command-line interface.
- CPU and GPU workload command-line interfaces retained from `D:\workload`.
- Platform, profile, calibration, baseline, run-manifest, and kernel-rule configuration interfaces.
- UART JSONL event interface.
- Qualification and production artifact interfaces.

Internal Python classes are described in the design document and are not a stable automation API unless later published separately.

## 2. Workflow overview

### 2.1 Baseline qualification

Use this workflow only on a representative known-good cohort or when an existing baseline is invalidated:

```powershell
vmin_judge.exe --transport hdc --device DEVICE_ID probe
vmin_judge.exe deploy --target all --verify-hashes
vmin_judge.exe golden cpu --profile cpu_mixed_big4 --runs 10 --known-good --board-id BOARD_001
vmin_judge.exe calibrate cpu --profile cpu_mixed_big4 --runs 30 --board-id BOARD_001 --golden GOLDEN_MANIFEST.json
vmin_judge.exe baseline approve BASELINE_ID --approver USER
```

GPU qualification uses `golden gpu` and `calibrate gpu`.

### 2.2 Batch testing

Batch testing consumes an approved baseline directly:

```powershell
vmin_judge.exe `
  --pc-serial '<PC_UART_PORT>' `
  --baudrate 9600 `
  --output-dir D:\avs-results `
  run `
  --profile cpu_mixed_big4 `
  --baseline auto
```

The tool performs a lightweight compatibility check, incrementally deploys missing/changed assets, verifies the environment, runs and monitors the workload, collects evidence, and reports the result. It does not regenerate golden data or recalibrate limits.

## 3. Packaged executable and global API

### API: `vmin_judge.exe`

**Purpose:** Provide the stable PC entry point for qualification, deployment, batch execution, monitoring, collection, validation, and reporting.

**Origin:** Extends the current Python `vmin_judge` CLI, which already exposes `pair`, `monitor`, `execute`, `simulate`, `list-profiles`, and `validate`.

**Rationale:** Operators and automation require one packaged interface with stable exit codes and no dependency on the source-tree working directory.

The operator normally starts this agent through `run`, `golden`, or `calibrate`; the PC has already resolved the JSON manifest and supplies a safe argument vector. The supported direct health check is:

```text
vmin_judge.exe [global-options] <command> [command-options]
```

### Global parameters

| Parameter | Purpose | Default/behavior |
|---|---|---|
| `--config-dir PATH` | Explicit external configuration override root. It may contain `config/platforms/...` or start directly at `platforms/...`. | Optional; overrides caller, executable, and bundled configuration. |
| `--output-dir PATH` | Root for qualification and run artifacts. | Current directory or configured writable state. |
| `--state-dir PATH` | Persistent pairing, baseline registry, and cache. | User-local application state. |
| `--transport auto\|adb\|hdc` | Device control transport. | `auto`. |
| `--device SERIAL` | ADB/HDC target identifier. | Auto-select only when unambiguous. |
| `--adb-bin PATH` | Explicit ADB executable. | Config, packaged tool, then `PATH`. |
| `--hdc-bin PATH` | Explicit HDC executable. | Config, packaged tool, then `PATH`. |
| `--device-root POSIX_PATH` | Remote deployment root. | `/data/local/tmp/avs`. |
| `--pc-serial PORT` | Actual PC-side UART enumerated by the host. | Saved pairing or command-specific requirement. |
| `--device-uart POSIX_PATH` | Device-side UART such as `/dev/ttyAMA0`. | Saved pairing or platform profile. |
| `--baudrate INTEGER` | UART baud. | Saved/platform value; Kirin9020 uses `9600`. |
| `--log-level LEVEL` | `debug`, `info`, `warning`, or `error`. | `warning`; routine `run` progress is not copied to CMD. |
| `--json` | Print machine-readable command result. | Off. |
| `--quiet` | Suppress progress output. | Off. |
| `--version` | Print packaged version and schema support. | Exits immediately. |

**Outputs:** Normal `run` writes operational events to UART/artifacts and prints only its final result. A failed run additionally prints concise actionable reason fields; `--json` keeps the same contract in machine-readable form.

**Errors:** Invalid configuration, ambiguous device, missing dependency/tool, unsupported device, transport failure, or command-specific failure. CMD receives the concise error code plus useful path/requested/actual fields; complete evidence remains in `result.json`.

`validate` and `probe` report the resolved platform/profile file path and SHA-256. Check those fields before trusting an override; deployment manifests do not prove platform-YAML selection because platform configuration is consumed on the PC and is not deployed to the device.

## 4. PC CLI command APIs

### API: `probe`

**Purpose:** Discover and normalize platform capabilities before deployment or testing.

**Origin:** Combines paths from `default.yaml` and `monitor_profiles.yaml` with existing serial/device discovery.

**Rationale:** Interface locations, permissions, thermal-zone indices, debugfs availability, and utility support must be validated rather than assumed.

**Usage:**

```powershell
vmin_judge.exe `
  --transport hdc `
  --device DEVICE_ID `
  probe `
  --platform kirin9020 `
  --full
```

| Parameter | Meaning |
|---|---|
| `--platform NAME` | Platform adapter/profile. |
| `--full` | Perform complete topology/interface/tool discovery. |
| `--refresh` | Ignore a cached capability record. |
| `--require NAME` | Require a named capability; repeatable. |

**Outputs:** `capabilities.json`, the resolved platform config path/fingerprint, device identity, interface provenance, units, permissions, and required/optional status. Thermal records retain the raw value, configured/applied unit, normalized Celsius value, and validity reason. Standalone `probe --full` scans both domains; profile-driven commands probe only their target domain and gate on that profile's requirements.

**Handoff:** Consumed by `deploy`, `golden`, `calibrate`, and `run`.

**Errors:** `UNSUPPORTED` when required hardware capability is absent; `INFRA_ERROR` for inaccessible or malformed interfaces.

### API: `pair`

**Purpose:** Determine which PC serial port is physically connected to which device UART.

**Origin:** Retains the current marker-based serial pairing feature.

**Rationale:** ADB/HDC controls the board while test events arrive through a separate physical UART.

**Usage:**

```powershell
vmin_judge.exe `
  --transport hdc `
  pair `
  --platform kirin9020 `
  --device-port '<DEVICE_UART_PATH>' `
  --pc-port '<PC_UART_PORT>' `
  --baudrate 9600 `
  --verify
```

| Parameter | Meaning |
|---|---|
| `--device-port PATH` | Optional explicit device UART. |
| `--pc-port PORT` | Optional explicit PC serial port. |
| `--platform NAME` | Optional device-specific UART candidates and baud-rate source. |
| `--timeout SECONDS` | Marker receive timeout. |
| `--verify` | Repeat a verification marker after pairing. |
| `--monitor` | Start diagnostic monitoring after success. |

**Outputs:** Persistent pairing record plus bounded `pair-diagnostic.json` evidence containing the failure class, marker write count, received-byte count, and a short hexadecimal preview.

**Handoff:** Used by later `run` or `monitor` commands when serial parameters are omitted.

**Behavior and errors:** The generic engine opens the selected PC port first, settles and clears stale bytes, retries a unique marker up to a bounded count, and accepts fragmented input. It does not guess host/device port names and does not reconfigure the device with `stty`. Errors distinguish missing candidates, busy/open failures, remote echo failure, zero received bytes, and received data without the marker.

### API: `deploy`

**Purpose:** Install or update device-agent, workload, shader, golden, rule, and configuration assets.

**Origin:** Builds on existing ADB/HDC `push` support and remote paths declared in `default.yaml`.

**Rationale:** Qualification and batch runs must use verified assets while avoiding unnecessary retransmission.

**Usage:**

```powershell
vmin_judge.exe deploy `
  --target all `
  --profile gpu_vulkan_mixed `
  --verify-hashes
```

| Parameter | Meaning |
|---|---|
| `--target cpu\|gpu\|all` | Asset family to deploy. |
| `--profile NAME` | Include profile-specific shaders/golden/config. |
| `--force` | Push even when hashes match. |
| `--verify-hashes` | Require remote SHA-256 verification. |
| `--clean-stale` | Remove only manifest-owned obsolete files. |

**Outputs:** `deployment-manifest.json` with local/remote paths, sizes, SHA-256 values, permissions, actions, and verification status.

**Handoff:** Consumed by `golden`, `calibrate`, and `run`.

**Errors:** Transfer failure, remote filesystem/permission failure, hash mismatch, or insufficient storage.

### API: `golden cpu`

**Purpose:** Establish a trusted deterministic CPU checksum for one correctness fingerprint.

**Origin:** Uses the existing CPU workload `--generate-golden` and `golden`/`summary` records.

**Rationale:** The workload can derive an expected value internally, but an externally approved checksum generated repeatedly on known-good boards is more robust for Vmin validation.

**Usage:**

```powershell
vmin_judge.exe golden cpu `
  --profile cpu_mixed_big4 `
  --runs 10 `
  --known-good `
  --board-id BOARD_001
```

| Parameter | Meaning |
|---|---|
| `--profile NAME` | CPU qualification profile. |
| `--runs N` | Golden repetitions on this board. |
| `--board-id ID` | Qualification cohort identity. |
| `--known-good` | Required acknowledgement that the board/environment is qualified. |
| `--accept-checksum HEX` | Optional previously expected checksum for comparison. |
| `--run-dir [BOARD_ID=]PATH` | Reuse an already collected qualified run; repeatable. If omitted, the command executes live runs. |

**Outputs:** CPU golden manifest, every emitted golden record, effective configuration, workload hash, accepted/rejected repeats, and fingerprint.

**Handoff:** Referenced by CPU calibration and approved CPU baselines.

**Errors:** Non-identical repeated checksums, environment violation, workload failure, missing golden/summary, or fingerprint conflict.

### API: `golden gpu`

**Purpose:** Establish a trusted GPU raw readback/checksum artifact for one correctness fingerprint.

**Origin:** Uses the GPU workload `--generate-golden`, `--golden-file`, and verifier implementation.

**Rationale:** Later GPU executions require a known output generated with identical API, mode, shader, render, texture, backend, and build behavior.

**Usage:**

```powershell
vmin_judge.exe golden gpu `
  --profile gpu_vulkan_mixed `
  --runs 10 `
  --known-good `
  --board-id BOARD_001
```

| Parameter | Meaning |
|---|---|
| `--profile NAME` | GPU qualification profile. |
| `--runs N` | Repetitions used to validate stability. |
| `--board-id ID` | Cohort identity. |
| `--known-good` | Required qualification acknowledgement. |
| `--run-dir [BOARD_ID=]PATH` | Reuse an already collected run containing the GPU readback; repeatable. |

**Outputs:** Raw GPU buffer such as `vulkan_mixed.rgba`, golden manifest, buffer SHA-256, workload/shader hashes, effective configuration, driver/build identity, and repeat comparison.

**Handoff:** Deployed for GPU calibration and batch testing and referenced by an approved baseline.

**Errors:** Inconsistent readbacks, missing readback, driver/backend error, environment violation, or incompatible fingerprint.

### API: `calibrate cpu`

**Purpose:** Measure CPU performance distributions and propose limits for a CPU baseline.

**Origin:** Uses existing CPU summary metrics and performance gates plus newly collected CPU telemetry.

**Rationale:** Production thresholds must reflect representative known-good devices and controlled environmental conditions.

**Usage:**

```powershell
vmin_judge.exe calibrate cpu `
  --profile cpu_mixed_big4 `
  --runs 30 `
  --board-id BOARD_001 `
  --golden D:\avs-results\qualification\cpu-golden.json `
  --temperature-range 35:60
```

| Parameter | Meaning |
|---|---|
| `--profile NAME` | Profile referencing an accepted CPU golden. |
| `--runs N` | Repeated performance runs. |
| `--board-id ID` | Cohort member. |
| `--temperature-range MIN:MAX` | Accepted temperature band in °C. |
| `--min-accepted N` | Minimum compliant samples. |
| `--policy FILE` | Statistical margin/percentile policy. |
| `--golden FILE` | Accepted CPU/GPU golden manifest used to bind correctness fingerprints. |
| `--run-dir [BOARD_ID=]PATH` | Reuse collected samples; repeat for a multi-board cohort. If omitted, execute live runs. |

**Outputs:** Per-run evidence, accepted/rejected sample table, aggregate distributions, proposed CPU limits, and draft baseline.

**Handoff:** Draft is reviewed with `baseline show` and promoted with `baseline approve`.

**Errors:** Insufficient accepted samples, golden mismatch, uncontrolled environment, telemetry loss, or unstable cohort.

### API: `calibrate gpu`

**Purpose:** Measure GPU performance/telemetry distributions and propose GPU limits.

**Origin:** Uses GPU workload frame/job metrics and Kirin/HVGR telemetry interfaces.

**Rationale:** GPU correctness alone does not detect abnormal FPS, latency, throttling, frequency, or stability.

**Usage:**

```powershell
vmin_judge.exe calibrate gpu `
  --profile gpu_vulkan_mixed `
  --runs 30 `
  --board-id BOARD_001 `
  --golden D:\avs-results\qualification\gpu-golden.json `
  --temperature-range 35:60
```

**Inputs:** Same common calibration controls as CPU plus a GPU golden, driver/API capability, shader hashes, and GPU-specific policy.

**Outputs:** FPS/frame/GPU-job distributions, telemetry distributions, accepted/rejected samples, proposed GPU limits, and draft baseline.

**Handoff:** Draft baseline review and approval.

**Errors:** Golden mismatch, device lost, GPU timeout, unsupported API, throttle/environment violation, or insufficient samples.

### API group: `baseline`

**Purpose:** Manage immutable qualification results and control which baseline production runs may consume.

**Origin:** New API required by the separation of qualification from batch testing.

**Rationale:** Calibration data must not be silently overwritten or used before human approval.

**Usage:**

```powershell
vmin_judge.exe baseline list --status approved
vmin_judge.exe baseline show BASELINE_ID
vmin_judge.exe baseline approve BASELINE_ID --approver USER
vmin_judge.exe baseline deprecate BASELINE_ID --reason "driver update"
```

| Subcommand | Purpose | Output |
|---|---|---|
| `list` | Search by profile/status/platform. | Baseline summaries. |
| `show ID` | Display fingerprints, cohort, limits, golden, and approval. | Full baseline document. |
| `approve ID` | Promote a draft after review. | Immutable approved version and approval record. |
| `deprecate ID` | Prevent new runs while preserving history. | Updated status/audit record. |
| `export ID` | Create a portable hash-verified baseline bundle. | Bundle path and hash. |
| `import FILE` | Validate and register a baseline bundle. | Registered ID or validation failure. |

**Errors:** Invalid transition, fingerprint inconsistency, missing golden, failed signature/hash, or unknown ID.

### API: `run`

**Purpose:** Execute one or more production tests against an approved baseline with integrated monitoring.

**Origin:** Replaces the prototype's split/duplicated `execute` and `monitor` behavior.

**Rationale:** Workload launch, event monitoring, policy evaluation, artifact collection, cleanup, and restoration form one transactional operation.

**Usage:**

```powershell
vmin_judge.exe `
  --pc-serial '<PC_UART_PORT>' `
  run `
  --profile gpu_vulkan_mixed `
  --baseline BASELINE_ID `
  --kernel-monitor critical `
  --repeat 1
```

| Parameter | Meaning |
|---|---|
| `--profile NAME` | Production profile. |
| `--baseline ID\|auto` | Explicit approved baseline or compatible auto-resolution. |
| `--repeat N` | Sequential repetitions. |
| `--run-id ID` | Optional externally supplied unique run ID. |
| `--no-deploy` | Require already matching device assets. |
| `--keep-device-spool` | Retain the device spool after PASS. Failures are always retained. |
| `--kernel-monitor critical\|off` | Filtered critical kernel policy. |
| `--overall-timeout SEC` | PC-side transaction timeout. |
| `--heartbeat-timeout SEC` | PC-side liveness timeout. |

**Outputs:** Complete PC run artifact directory and final machine-readable result. Routine events stay on UART/artifacts; CMD shows only the final verdict and, on failure, concise reasons. A PASS removes its device spool after the PC artifact is complete unless `--keep-device-spool` is set.

**Handoff:** Result/report goes to batch automation; artifacts go to traceability storage.

**Errors:** Any typed DUT verdict, infrastructure/configuration/unsupported error, user abort, or incompatible baseline.

### Compatibility API: `execute`

**Purpose:** Preserve legacy automation temporarily.

**Origin:** Existing command.

**Rationale:** Allows staged migration.

**Usage:** Same essential inputs as `run`; internally translated to `run` with integrated monitoring.

**Deprecation:** New automation must use `run`. Removal requires a major CLI version.

Legacy `--no-launch` and `--auto-pair` are rejected by the v2 alias with an actionable error. Use `monitor` for diagnostic-only serial capture and `pair` as an explicit step. Correctness-sensitive workload overrides belong in a versioned profile/configuration, which changes the fingerprint and requires qualification; production `run` does not accept ad-hoc profile overrides.

### API: `monitor`

**Purpose:** Diagnose or observe an already running device-agent event stream without launching a workload.

**Origin:** Retains the existing standalone serial-monitor concept.

**Rationale:** Useful for UART bring-up, protocol debugging, and controlled external launch systems.

**Usage:**

```powershell
vmin_judge.exe --pc-serial '<PC_UART_PORT>' --baudrate 9600 monitor --save-raw
```

| Parameter | Meaning |
|---|---|
| `--save-raw` | Preserve raw serial bytes. |
| `--expected-run-id ID` | Reject records from another run. |
| `--schema-version N` | Require a protocol major version. |
| `--timeout SEC` | Stop if no usable record arrives. |

**Outputs:** Decoded events and optional raw stream; no DUT PASS unless a complete compatible run manifest and terminal evidence are available.

Run `monitor` only while a device agent or other documented JSONL frame producer is active. Pairing markers are raw bring-up bytes, not protocol events.

**Errors:** Serial open/read failure, framing/sequence/schema error, or timeout.

### API: `collect`

**Purpose:** Pull device-spooled artifacts after a run or recover evidence after PC/UART interruption.

**Origin:** Builds on existing ADB/HDC `pull` support.

**Rationale:** Full logs should be retained on-device instead of flooding UART and must remain recoverable.

**Usage:**

```powershell
vmin_judge.exe collect --run-id RUN_ID --verify-hashes
```

| Parameter | Meaning |
|---|---|
| `--run-id ID` | Run to collect. |
| `--remote-run-dir PATH` | Optional explicit remote run directory. |
| `--verify-hashes` | Verify against device artifact manifest. |
| `--keep-remote` | Do not delete eligible remote spool data after success. |

**Outputs:** Local artifacts plus collection verification record.

**Errors:** Missing run, pull failure, hash mismatch, or incomplete remote manifest.

### API: `report`

**Purpose:** Regenerate a human/machine report from stored run artifacts without rerunning hardware.

**Origin:** Extends the current basic result formatter.

**Rationale:** Reporting format changes must not require another device execution.

**Usage:**

```powershell
vmin_judge.exe report --run-dir D:\avs-results\RUN_ID --format markdown,json,csv
```

**Outputs:** Requested reports; source artifacts remain unchanged.

**Errors:** Missing/inconsistent artifacts or unsupported schema.

### API: `validate`

**Purpose:** Validate configuration, profiles, baseline bundles, deployment assets, or packaged resources without running a test.

**Origin:** Retains and expands the existing `validate` command.

**Rationale:** Configuration and packaging failures should be detected before office hardware time is used.

**Usage:**

```powershell
vmin_judge.exe validate --all
vmin_judge.exe validate --profile cpu_mixed_big4
vmin_judge.exe validate --package
```

| Parameter | Meaning |
|---|---|
| `--all` | Validate all discoverable resources. |
| `--profile NAME` | Validate profile and referenced artifacts. |
| `--baseline ID\|FILE` | Validate fingerprints, approval, and hashes. |
| `--package` | Validate packaged resources and tool resolution. |
| `--offline` | Skip device-dependent checks. |

**Outputs:** Validation report and nonzero exit on errors.

### API: `list-profiles`

**Purpose:** List usable, pending, deprecated, or unsupported profiles.

**Origin:** Existing command.

**Rationale:** Operators need discoverable profile names and compatibility status.

**Usage:**

```powershell
vmin_judge.exe list-profiles --target cpu --status implemented
```

**Outputs:** Profile name, target, description, required baseline, status, and compatibility notes.

### API: `simulate`

**Purpose:** Replay saved JSONL or raw serial captures through the decoder and policy engine.

**Origin:** Existing log-file simulation command.

**Rationale:** Enables regression and failure-policy testing without connected UDP boards.

**Usage:**

```powershell
vmin_judge.exe simulate --events events.jsonl --profile cpu_mixed_big4
```

| Parameter | Meaning |
|---|---|
| `--events FILE` | Framed JSONL events. |
| `--raw-serial FILE` | Raw capture to decode. |
| `--profile NAME` | Policy/profile context. |
| `--baseline ID` | Optional approved baseline. |
| `--realtime` | Replay original timing instead of immediate processing. |

**Outputs:** Simulated result and decoder/policy statistics.

## 5. Device-agent CLI API

### API: `avs-device-agent`

**Purpose:** Execute one resolved run on the device and provide the only framed UART writer.

**Origin:** New component replacing independent workload and `dmesg` redirection processes.

**Rationale:** Multiple processes writing directly to one UART can interleave bytes and corrupt workload JSON.

**Usage:**

```sh
/data/local/tmp/avs/bin/avs-device-agent --version
```

| Parameter | Meaning |
|---|---|
Internal run parameters include run ID, target, UART, spool directory, timeout, environment actions, telemetry sources, and the workload argv after `--`. They are generated by the PC and are not a stable operator-facing interface.

The agent does not change the device UART baud automatically. The BSP/console owns that setup; `9600` configures the PC endpoint and records the expected link rate.
| `--version` | Print agent/protocol version. |

**Inputs:** PC-resolved run parameters, deployed workload/assets, platform interfaces, and kernel filter rules. The current Shell backend receives a safe flattened argv; it does not parse or require a remote JSON manifest.

**Outputs:** UART JSONL events, local device spool, agent process exit status.

**Errors:** Manifest/hash/config failure, environment apply/readback failure, workload failure, telemetry failure, UART failure, timeout, or restoration failure. Restoration failure is always reported even when the workload passed.

**Implementation note:** Monitor deploys one version-controlled Shell script and passes resolved data-only arguments. It does not deploy or generate a per-run script, and it no longer pushes an unused remote manifest. Only the agent writes the UART; workload, telemetry, and filtered kernel producers are framed through that writer.

## 6. Native CPU workload API

### API: `cpu-avs-workload`

**Purpose:** Generate deterministic CPU load, verify computation, measure performance, emit liveness/events, and return a detailed terminal status.

**Origin:** Existing C++ CPU workload under `D:\workload\cpuworkload`.

**Rationale:** Correctness and performance measurement belong close to the computation; platform policy such as affinity/frequency remains controlled by the device agent.

**Configuration precedence:** built-in profile defaults, then flat JSON `--config`, then CLI overrides.

**Usage:**

```sh
cpu-avs-workload \
  --config /data/local/tmp/avs/configs/cpu_mixed_big4.json \
  --output-format jsonl
```

### CPU parameter groups

| Group | Parameters | Usage |
|---|---|---|
| Selection | `--profile`, `--backend`, `--api cpu`, `--mode compute`, `--config` | Select deterministic backend and inputs. |
| Runtime | `--duration`, `--batches`, `--warmup`, `--timeout`, `--iterations`, `--threads`, `--working-set-kb`, `--seed`, `--batch-timeout-ms` | Define load and stop/timeout behavior. |
| Duty cycle | `--duty-cycle`, `--burst-period`, `--burst-active` | Define burst active/idle behavior. |
| Verification | `--verify-mode`, `--checksum-interval`, `--golden-checksum`, `--fail-fast`, `--generate-golden` | Generate or apply deterministic correctness data. |
| Monitoring | `--heartbeat-interval`, `--summary-only`, `--per-batch-log`, `--output-format jsonl`, `--output` | Control event volume and destination. |
| Performance | `--min-operations-per-sec`, `--max-throughput-cv-pct`, `--max-batch-p99-ms`, `--max-heartbeat-gap`, `--fail-on-instability` | Apply calibrated limits. |
| Utility | `--list-profiles`, `--dump-effective-config`, `--version`, `--help` | Inspect without testing. |

**Outputs:** JSONL `start`, `heartbeat`, optional `batch`, `verify`, `golden`, `error`, and `summary` records.

**Exit codes:** `0 PASS`, `1 CHECKSUM_FAIL`, `2 API_ERROR`, `3 TIMEOUT`, `4 DEVICE_LOST` reserved, `5 ALLOCATION_FAIL`, `6 UNKNOWN_ERROR`, `7 PERFORMANCE_FAIL`.

## 7. Native GPU workload API

### API: `gpu-avs-workload`

**Purpose:** Generate deterministic GPU graphics/compute load, read back and verify output, measure frames/GPU jobs, emit events, and return detailed status.

**Origin:** Existing C++ GPU workload under `D:\workload\gpuworkload`.

**Rationale:** API/backend-specific work and readback verification must execute on-device, while platform telemetry and final baseline policy remain external.

**Configuration precedence:** built-in profile defaults, then flat JSON `--config`, then CLI overrides.

**Usage:**

```sh
gpu-avs-workload \
  --config /data/local/tmp/avs/configs/gpu_vulkan_mixed.json \
  --output-format jsonl
```

### GPU parameter groups

| Group | Parameters | Usage |
|---|---|---|
| Selection | `--profile`, `--api`, `--mode`, `--config` | Select Vulkan/GLES/OpenCL/null and graphics/compute mode. |
| Render | `--width`, `--height`, `--rt-format`, `--samples` | Define output buffer identity. |
| Runtime | `--duration`, `--frames`, `--warmup`, `--timeout`, `--loop` | Define run length and timeout. |
| Load | `--shader`, `--shader-dir`, `--complexity`, `--iterations`, `--texture-count`, `--texture-size`, `--duty-cycle`, `--burst-period`, `--burst-active` | Define GPU work. |
| Verification | `--verify-mode`, `--checksum-interval`, `--golden-checksum`, `--golden-file`, `--pixel-threshold`, `--pixel-max-diff-count`, `--fail-fast`, `--generate-golden` | Generate/apply readback correctness data. |
| Timing | `--gpu-timestamp`, `--timestamp-scope`, `--gpu-timeout-ms` | Control GPU timing and timeout. |
| Monitoring | `--heartbeat-interval`, `--summary-only`, `--per-frame-log`, `--output-format`, `--output` | Control event volume and destination. `per-frame-log` is currently parsed but does not emit frame events; do not rely on it until implemented or formally deprecated. |
| Utility | `--list-profiles`, `--dump-effective-config`, `--version`, `--help` | Inspect without testing. |

**Outputs:** JSONL `start`, `heartbeat`, `verify`, `golden`, `error`, and `summary` records plus an optional raw golden file. A future explicit `frame` event requires a protocol revision and bandwidth policy.

**Important:** `verify_mode=none` performs no correctness validation. Checksum without a trusted checksum records rather than validates. Exact/pixel/compute comparison requires a compatible golden file.

## 8. Configuration APIs

### API: Platform YAML

**Purpose:** Describe stable hardware interfaces, transport defaults, units, fallbacks, and required privileges.

**Origin:** Refactors `default.yaml` and `monitor_profiles.yaml` into a validated platform adapter.

**Rationale:** Single- and dual-framework boards may share paths while differing in transport or permissions; probing verifies the contract.

**Usage:** Selected by `--platform` or referenced by a profile.

**Output/handoff:** Provides candidates to `probe`; resolved choices are written to `capabilities.json`.

### API: Profile YAML

**Purpose:** Bind workload configuration, environment, golden reference, performance limits, required capabilities, and failure policy to a stable name.

**Origin:** Replaces the empty/incomplete `workload_profiles.yaml` while retaining named profile selection.

**Rationale:** A production test must be one reproducible contract rather than unrelated command-line fragments.

**Usage example:**

```yaml
schema_version: 1
name: cpu_mixed_big4
target: cpu
platform: kirin9020
workload:
  binary: bin/cpu-avs-workload
  config: workloads/cpu_mixed_big4.json
environment:
  affinity: "4-7"
  governor: performance
  temperature_c: {min: 35, max: 60}
baseline: kirin9020-cpu-mixed-big4-v1
telemetry:
  interval_ms: 1000
  required: [cpu.frequency, cpu.online, cpu.temperature]
kernel_monitor: critical
kernel_options: {dedupe_window_ms: 1000, max_events_per_second: 10}
```

**Handoff:** Resolved with baseline/capabilities into a run manifest and flat workload JSON.

### API: Calibration policy YAML

**Purpose:** Define cohort/sample requirements and approved statistical margin methods.

**Origin:** New qualification requirement.

**Rationale:** Threshold derivation must be repeatable and reviewed, not encoded as arbitrary code constants.

**Usage:** Passed through `calibrate --policy`.

**Outputs:** Policy identity/hash is stored in the draft baseline.

### API: Baseline JSON

**Purpose:** Store an immutable approved correctness/performance contract.

**Origin:** New separation between qualification and batch execution.

**Rationale:** Batch testing directly reuses selected-board baseline data only when fingerprints match.

**Required content:** ID/version/status, platform scope, correctness fingerprint, performance fingerprint, golden reference/hash, thresholds, environment, cohort/sample statistics, workload/shader hashes, schema versions, approval identity/time, and optional signature.

**Usage:** Resolved explicitly with `--baseline ID` or compatibly with `--baseline auto`.

### API: Run-manifest JSON

**Purpose:** Provide the device agent with complete instructions for exactly one run.

**Origin:** Replaces dynamically constructed shell command fragments and generated scripts.

**Rationale:** A resolved immutable manifest is auditable, hashable, and safe to validate before mutation.

**Required content:** Run ID, profile/baseline IDs and hashes, target, workload argv, asset paths/hashes, UART/spool paths, environment actions/readbacks, telemetry samplers, kernel rules, timeouts, event schema, and restoration plan.

**Usage:** Generated by PC `run`, pushed to the remote run directory, and passed to `avs-device-agent --manifest`.

### API: Kernel rule configuration

**Purpose:** Identify only critical kernel events to report live and optional warning/ignore patterns.

**Origin:** Refines current `cpu_judge.conf`/GPU rules while removing raw dmesg transmission.

**Rationale:** Kernel evidence is valuable, but unrelated kernel traffic can exhaust UART bandwidth and corrupt workload delivery.

**Usage:** Deployed to the agent and referenced by the run manifest. Full raw logs remain on-device.

`dmesg` is not needed to judge workload correctness, performance, or telemetry limits. It is required only when `kernel_monitor` is `critical` or `full-local`; use `off` on images that do not expose it. Live kernel matches are deduplicated and rate-limited by `kernel_options`. `full-local` additionally writes `dmesg.raw` to the device spool but still does not send raw kernel traffic over UART.

## 9. UART event API

### Event envelope

**Purpose:** Frame and route every live record reliably.

**Origin:** Wraps existing workload JSONL and new agent/telemetry/kernel events.

**Rationale:** Run ID, sequence, source, schema, timestamp, and optional CRC allow the PC to detect contamination, loss, and corruption.

```json
{
  "schema_version": 1,
  "run_id": "RUN_ID",
  "seq": 1,
  "timestamp_ms": 1234,
  "source": "agent",
  "type": "agent_start",
  "payload": {},
  "crc32": "optional"
}
```

### Event-type APIs

| Type | Purpose | Origin | Rationale and usage |
|---|---|---|---|
| `agent_start` | Declare agent/version/manifest identity. | New. | First event; PC rejects incompatible run/schema. |
| `capability` | Report resolved interface/tool capability. | Platform probing. | Proves which interface produced each metric. |
| `environment` | Report requested/applied/read-back environment. | New environment controller. | Required before workload start; mismatch may block run. |
| `start` | Report effective workload configuration. | Existing CPU/GPU workload. | PC compares it with run manifest/baseline. |
| `heartbeat` | Report liveness and rolling progress/performance. | Existing workloads. | Resets watchdog and feeds real-time limits. |
| `batch` | Optional CPU batch detail. | Existing CPU workload. | Normally suppressed to protect bandwidth. |
| `verify` | Report correctness comparison. | Existing workloads. | Any failed verify is a DUT failure. |
| `golden` | Report generated correctness artifact identity. | Existing workloads. | Qualification only; stored by GoldenService. |
| `telemetry` | Report normalized CPU/GPU frequency/temperature/etc. | New agent sampler. | Compared with environmental and performance policy. |
| `kernel` | Report filtered critical/warning kernel event. | Existing rules, new filter. | Critical match can fail DUT; no raw stream. |
| `error` | Report workload/agent/platform error. | Existing workloads plus agent. | Routed by source/type to DUT or infrastructure policy. |
| `summary` | Report authoritative workload result and metrics. | Existing workloads. | Non-`PASS` or nonzero exit is terminal failure. |
| `violation` | Report a policy limit crossing with evidence. | New policy/agent. | Supports real-time display and final reasoning. |
| `agent_final` | Report workload exit, cleanup, restoration, spool state. | New. | Required final agent event; restoration failure is preserved. |

Unknown additive fields are retained. An unsupported major schema, invalid JSON, sequence gap, wrong run ID, or CRC failure is an infrastructure error.

## 10. Artifact APIs

### API: Qualification directory

**Purpose:** Preserve cohort, golden, sample, calibration, proposal, and approval evidence.

**Origin:** New baseline lifecycle.

**Rationale:** Approved limits must remain auditable and reproducible.

**Usage:** Created by `golden`/`calibrate`; read by `baseline approve` and later audit/export.

### API: Production run directory

**Purpose:** Preserve complete evidence for one test execution.

**Origin:** Expands current raw/log/result output.

**Rationale:** Console output and a reduced verdict are insufficient for reproducibility, failure analysis, or baseline audit.

**Required files:** Run manifest, capabilities, deployment manifest, effective profile/workload, events, telemetry, kernel events, workload summary, result, optional raw serial, artifact hashes, and report.

**Usage:** Consumed by automation, `report`, `simulate`, audit, and failure analysis.

### API: `result.json`

**Purpose:** Provide the canonical machine-readable final result.

**Origin:** Replaces the current reduced verdict-only formatting.

**Rationale:** Automation needs one canonical result that preserves workload details, evaluated limits, evidence, and infrastructure validity without scraping text.

**Required content:** Run/profile/baseline IDs, overall verdict/exit code, DUT and infrastructure reasons, workload result/exit, correctness evidence, performance metrics/limits, telemetry violations, kernel evidence, liveness, artifact completeness/hashes, timestamps, and tool versions.

**Usage:** Batch automation uses this file rather than scraping console text.

## 11. Process exit codes

| Code | Meaning | Usage |
|---:|---|---|
| 0 | PASS | Completed and satisfied the approved baseline. |
| 1 | DUT_FAIL | Correctness, performance, or critical platform failure. |
| 2 | SILENT_FAILURE | Heartbeat/result disappeared under an otherwise valid monitor. |
| 3 | INFRA_ERROR | Transport, framing, agent, artifact, or restoration failure prevents a reliable DUT judgement. |
| 4 | INVALID_CONFIGURATION | Profile, baseline, schema, path, or argument error. |
| 5 | UNSUPPORTED | Device lacks a required capability or compatible baseline. |
| 6 | USER_ABORT | Operator cancellation; cleanup/restoration still attempted. |

The native workload exit code is preserved separately in `result.json`.

## 12. Path-resolution API after packaging

### Purpose

Make commands behave identically from source, PyInstaller one-folder, and PyInstaller one-file packages.

### Origin

The prototype used the executable/script directory and changed the working directory, but did not distinguish PyInstaller `_MEIPASS`, external overrides, writable state, and outputs. The v2 `PathResolver` replaces that behavior.

### Rationale

One-file bundled resources are extracted to a temporary read-only lifecycle, while users need stable external configuration and writable results.

### Input resolution

1. Absolute CLI path.
2. Path relative to the caller's current working directory.
3. Path relative to `--config-dir`.
4. External override under the executable directory.
5. Bundled read-only default.

A path referenced inside a YAML/JSON file is resolved relative to that owning file.

### Output resolution

Relative outputs are created under `--output-dir`. Persistent baseline/pairing/cache data uses `--state-dir`. The executable and PyInstaller bundle directories are not assumed writable.

Release packaging requires staged device assets under the project `tools` directory: CPU and GPU workload binaries plus `fullscreen.vert.spv` and `workload.frag.spv`. The profile deploys the shaders to `/data/local/tmp/avs/shaders/vulkan`, and the GPU config exposes that path through `shader_dir`. Workload, config, and shader byte hashes are part of the correctness fingerprint, so changing any of them requires a new golden and calibration.

### Remote paths

Remote device paths always use POSIX semantics and are never normalized as Windows paths.

### Usage example

```powershell
Set-Location C:\automation\job-17
D:\tools\vmin_judge.exe `
  --config-dir D:\avs-config `
  --output-dir D:\avs-results `
  run --profile cpu_mixed_big4
```

The executable must resolve configuration from `D:\avs-config`, write to `D:\avs-results`, and never change the caller's current directory.

## 13. API stability and versioning

### Purpose

Allow packages, device agents, configurations, events, baselines, and automation to evolve without silent incompatibility.

### Origin

New requirement introduced by reusable baseline qualification and separately packaged PC/device components.

### Rationale

A baseline or event produced by one schema must not be accepted by an incompatible consumer merely because field names look similar.

### Usage

- CLI breaking changes increment the packaged CLI major version.
- Configuration, event, manifest, baseline, and result documents carry `schema_version`.
- Additive optional fields may be introduced within one major schema.
- Removing/changing a field or its unit requires a major schema version.
- The PC and device agent exchange supported protocol ranges before a run.
- Deprecated commands/options remain available for at least one documented migration period.
- Every artifact records producer name/version and applicable schema versions.

## 14. Operational guidance

- Use `validate --package --offline` before taking a new package to the office.
- When using `--config-dir`, inspect `resolved_configs.path`/`sha256` from `validate` and `platform_config` from `probe`; do not infer selection from deploy output.
- Use `probe --full` after a BSP/kernel/driver update.
- Use `deploy --verify-hashes`; later runs can deploy incrementally.
- Generate and calibrate only on representative known-good boards.
- Approve a baseline before batch use.
- Use `run --baseline auto` only when fingerprint matching is strict and unambiguous.
- Keep `--per-batch-log` and `--per-frame-log` disabled on a 9600-baud UART unless a dedicated bandwidth test proves safety.
- Use workload heartbeats plus normalized telemetry; do not stream raw dmesg.
- Treat missing/corrupt events as infrastructure errors and preserve raw captures for diagnosis.
