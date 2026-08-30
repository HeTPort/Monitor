# Vmin Monitor Refactoring and Development Design

Status: Implemented refactor with hardware/release validation remaining

Audience: Monitor developers, CPU/GPU workload developers, validation engineers, release engineers, and test operators

Companion document: [USER_GUIDE.md](USER_GUIDE.md)

## 1. Purpose

This document defines the refactoring and development work required to turn the current Monitor prototype into a reproducible CPU/GPU validation system for near-production Kirin 9020 UDP boards running HarmonyOS in single-framework and dual-framework configurations.

The finished system must:

- Qualify reusable correctness and performance baselines on representative known-good boards.
- Apply approved baselines directly during later batch testing without recalibrating every board.
- Deploy versioned workloads, shaders, golden artifacts, rules, and a device collector through ADB or HDC.
- Execute CPU and GPU workloads under controlled conditions.
- Collect workload events and real-time CPU/GPU telemetry without flooding or corrupting the serial stream.
- Use critical filtered kernel events as supplemental failure evidence rather than transmitting raw `dmesg`.
- Distinguish device-under-test failures from monitoring and infrastructure failures.
- Preserve complete, traceable artifacts for every qualification and production run.
- Work correctly as Python source and as a packaged executable, including PyInstaller one-file extraction and external configuration overrides.

## 2. Origin and rationale

The design originates from four existing assets and the gaps discovered between them:

1. The PC-side Monitor prototype already provides ADB/HDC channels, serial pairing, workload command construction, heartbeat handling, pattern rules, and basic result formatting.
2. The CPU and GPU workloads already provide flat-JSON/CLI configuration and JSONL events such as `start`, `heartbeat`, `verify`, `error`, and `summary`.
3. `default.yaml` identifies useful Kirin 9020/HVGR frequency, utilization, temperature, throttle, hang, power-policy, OPP, and voltage interfaces.
4. The current integration forwards workload output and `dmesg` independently to one UART, parses JSON with regular expressions, has no real checked-in workload profiles, and does not preserve detailed workload results or metrics.

The design therefore retains the useful transport and workload contracts while replacing the orchestration, event framing, baseline lifecycle, deployment, telemetry, path handling, and result model.

### 2.1 Implementation status (2026-08-23)

| Area | Status | Evidence/remaining work |
|---|---|---|
| Path, config, event, policy, artifact, baseline, and calibration services | Implemented | Covered by offline unit and integration tests. |
| ADB/HDC transport and deployment | Implemented | Typed argv-only commands, SHA-256 verification, idempotency, and managed stale cleanup are fake-transport tested. |
| PC qualification and production CLI | Implemented | `probe`, `deploy`, `golden`, `calibrate`, `baseline`, `run`, `collect`, `report`, `validate`, and `simulate` route through v2 services. |
| Device agent | Shell bring-up implementation complete | Fixed POSIX Shell script, single UART writer, telemetry/kernel producers, spool hashes, and restoration are offline tested; device Python is not required. |
| Packaging | Build definition complete | PyInstaller spec, bundled resources, hidden imports, build script, and frozen-path unit test exist. Build/smoke test still needs an environment with the packaging dependencies. |
| UDP hardware qualification | Pending office execution | Probe both framework variants, validate permissions/interfaces, generate CPU/GPU goldens, calibrate a known-good cohort, approve baselines, and run deliberate-failure tests. |

## 3. Scope

### 3.1 In scope

- PC CLI and orchestration refactoring.
- CPU/GPU platform probing.
- Idempotent deployment and integrity checks.
- CPU checksum-golden management.
- GPU raw-readback/golden management.
- Repeated calibration and statistical threshold derivation.
- Baseline review, approval, versioning, invalidation, and drift checks.
- Production execution and concurrent monitoring.
- On-device telemetry and critical kernel filtering.
- Unified JSONL event protocol and artifact schemas.
- Result policy and reporting.
- Packaged executable and deterministic path resolution.
- Simulation, regression, packaging, and hardware-in-the-loop tests.

### 3.2 Out of scope

- Automatically deciding safe voltage changes without an approved voltage-control specification.
- Treating uncalibrated example thresholds as production limits.
- Guaranteeing identical golden artifacts across incompatible compiler, workload, driver, shader, or hardware revisions.
- Sending unrestricted raw kernel logs over the workload UART.
- Modifying HarmonyOS kernel interfaces.

## 4. Terminology

| Term | Meaning |
|---|---|
| Qualification | Golden generation and repeated calibration on representative known-good boards. |
| Baseline | An immutable, approved correctness and performance contract. |
| Correctness fingerprint | Inputs that determine whether a golden checksum or buffer remains valid. |
| Performance fingerprint | Inputs and environment that determine whether performance limits remain valid. |
| Profile | A named test configuration referencing workload settings, environment, golden data, and limits. |
| Run manifest | Fully resolved instructions for one device execution. |
| Device agent | The sole on-device owner of workload launch, telemetry collection, event framing, UART output, and restoration. |
| DUT failure | Confirmed correctness, liveness, performance, or critical platform failure. |
| Infrastructure error | Invalid configuration, corrupt transport, missing assets, collector failure, or other inability to judge the DUT. |

## 5. Lifecycle model

### 5.1 Qualification workflow

```text
probe known-good cohort
  -> deploy qualified assets
  -> generate/repeat golden
  -> run repeated calibration
  -> reject non-compliant samples
  -> derive proposed limits
  -> human review and approval
  -> publish immutable baseline
```

Qualification is performed only when a baseline does not exist, is intentionally revised, or is invalidated by a compatibility change or drift policy.

### 5.2 Batch workflow

```text
load approved baseline
  -> target/profile-scoped compatibility probe
  -> verify or incrementally deploy assets
  -> verify environment
  -> execute workload and monitor events
  -> retain device spool on failure; remove it after a complete PASS unless requested
  -> judge against baseline
  -> report and archive
```

Batch testing does not generate a golden or derive thresholds.

### 5.3 Requalification triggers

Correctness requalification is required when any correctness fingerprint changes, including workload binary hash, backend, seed, threads, iterations, working set, compiler-sensitive behavior, GPU API/mode, render dimensions/format, shaders, texture configuration, verification algorithm, or golden file.

Performance requalification is required when any performance fingerprint changes, including SoC revision, kernel/BSP/driver, affinity, online CPUs, frequency/governor policy, thread count, temperature policy, power policy, telemetry interpretation, test fixture, or statistically significant reference-board drift.

## 6. Target architecture

```text
Packaged PC CLI
  |
  +-- Path/config resolver
  +-- Transport manager (ADB/HDC)
  +-- Platform probe
  +-- Deployment manager
  +-- Golden/calibration services
  +-- Baseline registry
  +-- Run orchestrator
  +-- Serial event decoder
  +-- Policy engine
  +-- Artifact store/reporting
  |
  +------ ADB/HDC control channel ------ Device agent
  |                                         |
  |                                         +-- environment controller
  |                                         +-- CPU/GPU workload process
  |                                         +-- telemetry samplers
  |                                         +-- critical kernel filter
  |                                         +-- local artifact spool
  |
  +------ UART event channel <-------------- single framed-event writer
```

The device agent is the only process permitted to write to the workload UART. Workload stdout is piped into the agent, preventing byte-level interleaving with telemetry or kernel messages.

## 7. Component design

### 7.1 `PathResolver`

Purpose: resolve bundled resources, external overrides, state, output, local tools, and remote POSIX paths deterministically.

Origin: replaces `_get_tool_dir()`, fallback searches, and `os.chdir(tool_dir)` in the prototype.

Key interface:

```python
class PathResolver:
    def resolve_input(self, value: str, *, owner: Path | None = None) -> Path: ...
    def resolve_resource(self, relative: str) -> Path: ...
    def resolve_output(self, relative: str) -> Path: ...
    def resolve_tool(self, name: str, explicit: str | None = None) -> Path: ...
    def remote(self, relative: str) -> PurePosixPath: ...
```

Rationale: packaged one-file resources are read-only and temporary, while configuration overrides, persistent state, and run outputs require stable writable locations.

### 7.2 `TransportManager`

Purpose: provide one typed ADB/HDC interface for connect, invoke, push, pull, hash, and process control.

Origin: refactors the current `ChannelManager`, `ADBChannel`, and `HDCChannel`.

Key interface:

```python
class Transport:
    def connect(self) -> DeviceIdentity: ...
    def invoke(self, argv: list[str], timeout_s: float) -> CommandResult: ...
    def push(self, local: Path, remote: PurePosixPath) -> TransferResult: ...
    def pull(self, remote: PurePosixPath, local: Path) -> TransferResult: ...
    def sha256(self, remote: PurePosixPath) -> str: ...
```

Rationale: structured argument lists and typed results are safer than concatenating shell strings and treating workload exit codes as transport failures.

Serial pairing is a separate device-agnostic transaction. The engine consumes discovered or operator/platform-supplied candidates, opens all eligible PC ports before device transmission, waits for driver settle, clears stale input, retransmits a unique marker within one bounded timeout, and matches across fragmented reads. It never guesses product-specific device nodes or host port numbers. A bounded diagnostic record classifies remote-write, port-open/busy, zero-RX, and marker-mismatch failures without dumping arbitrary UART contents.

### 7.3 `PlatformProbe`

Purpose: discover device identity, CPU topology, GPU driver, telemetry paths, permissions, utilities, serial UARTs, and capability versions.

Origin: consolidates paths from `default.yaml`, `monitor_profiles.yaml`, serial discovery, and runtime probing.

Output: versioned `capabilities.json` with required/optional capability status and normalized units. Thermal evidence retains raw values, configured/applied parsers, normalized Celsius values, and validity. Mixed degree/millidegree sources are normalized per path rather than by one platform-wide assumption.

Rationale: thermal-zone numbers, debugfs availability, devfreq aliases, units, and permissions may change between builds even when single-/dual-framework hardware interfaces are intended to match. Platform-wide missing requirements remain visible, but execution support is recomputed from the selected profile's requirement scope so an unrelated GPU-only gap does not block CPU qualification.

### 7.4 `DeploymentManager`

Purpose: compute an asset plan, push only missing/changed files, set permissions, and verify remote hashes.

Origin: builds on existing channel `push()`/`pull()` functions and the asset paths in `default.yaml`.

Output: `deployment-manifest.json` recording local/remote paths, hashes, versions, modes, and verification results.

Rationale: qualification and batch runs must execute known assets without repeatedly transferring unchanged files.

### 7.5 `GoldenService`

Purpose: generate, repeat, validate, and store CPU and GPU golden artifacts.

CPU behavior:

- Run `--generate-golden` on known-good boards.
- Require repeated identical 64-bit checksums across the accepted cohort.
- Store checksum and correctness fingerprint in a CPU golden manifest.

GPU behavior:

- Run `--generate-golden` with an exact configuration.
- Store the raw readback buffer, SHA-256, dimensions/format, shader hashes, build/driver identity, and correctness fingerprint.
- Require repeat consistency appropriate to the selected checksum/exact/pixel-diff/compute-compare mode.

Rationale: CPU correctness is represented by a deterministic reduced value; GPU correctness may require a raw readback artifact.

### 7.6 `CalibrationService`

Purpose: execute repeated known-good runs, validate environmental compliance, aggregate metrics, and propose performance limits.

Inputs: approved golden, cohort, run count, environmental constraints, statistical policy, and profile.

Outputs:

- Per-run manifests, events, telemetry, and summaries.
- Accepted/rejected sample reasons.
- Aggregate distributions.
- Proposed limits and margin calculations.
- Proposed baseline awaiting review.

Rationale: thresholds must come from representative measurements, not example constants or a single board.

### 7.7 `BaselineRegistry`

Purpose: store immutable baseline versions and manage draft, approved, deprecated, and invalid states.

Key interface:

```python
class BaselineRegistry:
    def create_draft(self, calibration: CalibrationResult) -> Baseline: ...
    def approve(self, baseline_id: str, approver: str) -> Baseline: ...
    def resolve(self, profile: str, fingerprint: Fingerprint) -> Baseline: ...
    def deprecate(self, baseline_id: str, reason: str) -> Baseline: ...
```

Rationale: batch runs must never silently consume unreviewed or overwritten calibration data.

### 7.8 `RunOrchestrator`

Purpose: build a local resolved run manifest, verify compatibility, deploy/verify assets, open and clear the PC UART before agent launch, receive/drain events, enforce independent workload liveness, finalize artifacts, and restore state.

Origin: replaces the duplicated `monitor` and `execute` loops plus the synchronous/background ambiguity in the current scheduler.

Rationale: execution and monitoring are concurrent parts of one transaction, with a single cleanup path.

### 7.9 `EventDecoder`

Purpose: incrementally decode UTF-8 JSONL envelopes, verify run ID/sequence/schema/CRC, preserve unknown additive fields, and route typed events.

Origin: replaces regular-expression extraction of JSON objects from an arbitrary byte buffer.

Rationale: a serial protocol must detect corruption and sequence loss instead of converting transport damage into an incorrect DUT failure.

### 7.10 `PolicyEngine`

Purpose: combine workload correctness, performance limits, telemetry constraints, critical kernel events, liveness, and infrastructure validity into a typed result.

Rationale: the workload's detailed terminal results must be preserved; every nonzero workload exit and every non-`PASS` terminal result is meaningful.

Verdict classes:

```text
PASS
DUT_FAIL
SILENT_FAILURE
INFRA_ERROR
INVALID_CONFIGURATION
UNSUPPORTED
USER_ABORT
```

### 7.11 `ArtifactStore`

Purpose: create atomic run directories, stream events, pull device artifacts, compute hashes, and produce result/report files.

Rationale: each verdict must be reproducible from immutable evidence.

## 8. Device agent design

### 8.1 Remote layout

```text
/data/local/tmp/avs/
  bin/avs-device-agent
  bin/cpu-avs-workload
  bin/gpu-avs-workload
  shaders/
  golden/cpu/
  golden/gpu/
  rules/kernel-critical.conf
  configs/
  runs/<run-id>/
```

### 8.2 Responsibilities

- Consume PC-resolved data-only arguments and execute verified deployed assets; the Shell backend does not parse a remote manifest.
- Capture the original affinity, governor, frequency, and related mutable state.
- Apply requested environment and verify readback.
- Emit structured environment readback/restoration evidence with path, requested value, and actual value.
- Launch the workload with stdout/stderr captured through a pipe.
- Sample CPU/GPU telemetry at configured intervals.
- Monitor filtered critical kernel events without clearing the kernel ring buffer.
- Add event envelope metadata and serialize through one UART writer.
- Spool full local events and raw logs for post-run collection.
- Emit an agent-final event with workload exit status.
- Terminate child processes and restore device state on every exit path.

### 8.3 Production form

A small native executable is preferred. Fixed version-controlled shell scripts may be used for initial bring-up, but generated per-run shell code is not the long-term contract. Per-run variability belongs in the run manifest.

## 9. CPU operation design

### 9.1 Correctness qualification

Correctness fingerprint fields include workload hash, backend/profile, seed, threads, iterations, working set, verification mode, target architecture, toolchain, and floating-point settings where applicable.

The CPU golden is a checksum manifest rather than a raw output file. Production runs pass the trusted `golden_checksum`, check every batch internally, emit failed verification immediately, and may reduce successful verification emission with `checksum_interval`.

### 9.2 Performance qualification

Calibration fixes affinity, cluster, online CPU set, governor/frequency, thread count, warm-up, duration, temperature band, and workload arguments. It records operations/sec, heartbeat-window distribution, batch latency distribution, heartbeat gaps, and telemetry compliance.

Supported CPU gates retained from the workload are:

- Minimum overall operations/sec.
- Maximum heartbeat-window throughput coefficient of variation.
- Maximum batch p99 latency.
- Maximum heartbeat gap.
- Optional conversion of violations to `PERFORMANCE_FAIL`.

Burst profiles require a separate policy because intentional active/idle transitions make a low throughput-CV limit inappropriate.

## 10. GPU operation design

### 10.1 Correctness qualification

Correctness fingerprint fields include workload hash, API, mode, profile, shader and shader hashes, width, height, render-target format, samples, iterations, texture configuration, verification mode, golden hash, driver/backend identity, and relevant build identity.

GPU verification modes are checksum/CRC, exact golden file, pixel-diff, or compute-compare. Checksum without a trusted checksum is recording, not validation. Raw `.rgba` artifacts are opaque GPU readback buffers and are not assumed to be encoded images.

### 10.2 Performance qualification

GPU calibration records FPS, frame-time distribution, GPU-job distribution, frequency, utilization, temperature, throttle duration, hang/reset/fault counters, power policy, and voltage where available.

The GPU workload currently lacks CPU-style performance gates. The implementation must either add minimum FPS, maximum frame/GPU-job p99, maximum variability, heartbeat-gap, and throttle-duration inputs to the GPU workload or implement equivalent final evaluation in the PC policy engine. Real-time hard failures remain in the PC policy engine in either case.

The current GPU CLI parses `per_frame_log`, but the logger/runner does not emit a frame event. The refactoring must either implement a documented `frame` event with an explicit bandwidth policy or deprecate the option. Production monitoring must not assume that enabling the existing option produces frame records.

## 11. Telemetry and kernel policy

### 11.1 CPU telemetry

- Per-policy/cluster current frequency and limits.
- Related/affected CPU set.
- Per-core online state.
- `/proc/stat` utilization deltas.
- Optional cpuidle residency.
- CPU/SoC thermal zones resolved by type.
- Cooling/throttle state where exposed.
- Voltage/readback where exposed and parsed by a platform adapter.

### 11.2 GPU telemetry

- Current frequency, preferring devfreq and falling back to HVGR debugfs or `freqdump`.
- Utilization.
- GPU thermal zone resolved by type.
- Throttle state/duration.
- Hang/reset/fault counters.
- Power policy.
- Voltage/readback where exposed.

### 11.3 Kernel monitoring

Raw `dmesg` is not part of the UART protocol. The agent filters, deduplicates, and rate-limits critical patterns such as panic/Oops, lockup/watchdog/RCU stall, GPU hang/reset, SMMU/IOMMU fault, ECC/bus error, and power/undervoltage failure. Full logs are retained on-device and pulled after the run when available.

## 12. Configuration model

The PC accepts structured YAML for orchestration and generates flat JSON for the existing CPU/GPU workload CLIs.

```text
config/platforms/<platform>.yaml
config/workloads/cpu.yaml
config/workloads/gpu.yaml
config/profiles/<profile>.yaml
config/calibration-policy.yaml
config/kernel-critical.conf
baselines/<baseline-id>/baseline.json
```

Configuration precedence is:

```text
bundled defaults
  -> external platform/workload/profile files
  -> approved baseline values
  -> explicitly permitted run overrides
```

Correctness-critical changes are made in a versioned profile/configuration. They change the fingerprint and require a matching newly qualified baseline; production runs reject ad-hoc overrides.

Serial candidates and baud rates are platform data, not pairing-engine constants. `config/platforms/<platform>.yaml` may declare them for that device family; explicit operator values take precedence, and an unselected platform cannot inject its UART paths into generic discovery.

## 13. Event protocol

Every UART record is one UTF-8 JSON object followed by LF:

```json
{
  "schema_version": 1,
  "run_id": "20260823-001",
  "seq": 127,
  "timestamp_ms": 184920,
  "source": "cpu-workload",
  "type": "heartbeat",
  "payload": {},
  "crc32": "optional"
}
```

Required sources are `agent`, `cpu-workload`, `gpu-workload`, `cpu-telemetry`, `gpu-telemetry`, and `kernel`.

Required event types are `agent_start`, `capability`, `environment`, `start`, `heartbeat`, `batch`, `verify`, `golden`, `telemetry`, `kernel`, `error`, `summary`, `violation`, and `agent_final`.

Schema changes are additive within a major version. Unknown fields are preserved; an unsupported major version is an infrastructure error.

## 14. Artifacts and handoffs

### 14.1 Qualification artifacts

```text
qualification/<qualification-id>/
  cohort.json
  capabilities/
  golden/
  samples/<run-id>/
  calibration.json
  proposed-baseline.json
  approval.json
```

### 14.2 Production run artifacts

```text
output/<run-id>/
  run-manifest.json
  capabilities.json
  deployment-manifest.json
  effective-profile.yaml
  effective-workload.json
  events.jsonl
  telemetry.jsonl
  kernel-events.jsonl
  workload-summary.json
  result.json
  serial.raw
  artifact-hashes.json
  report.md
```

Writes use temporary names and atomic rename where supported. `result.json` is written only after artifact finalization, or marked incomplete with an infrastructure error.

## 15. Packaged path design

Four roots are maintained explicitly:

| Root | Purpose |
|---|---|
| `bundle_root` | Read-only resources bundled by PyInstaller; `_MEIPASS` for a frozen one-file build. |
| `exe_root` | Directory containing the packaged executable or source entry point. |
| `state_root` | Persistent writable pairing, registry, cache, and application state. |
| `output_root` | User-selected or default per-run result location. |

Input resolution order is absolute CLI path, current working directory, `--config-dir`, external `exe_root/config` override, then bundled default. A path referenced by a configuration file is resolved relative to that file's directory.

The application must not call `os.chdir()`. It must not write into `bundle_root`. Remote paths use `PurePosixPath`; local paths use `Path`. A relative output path is resolved under `--output-dir`, not under the executable directory.

ADB/HDC resolution order is explicit CLI path, configured path, bundled/external `tools` directory, then system `PATH`.

## 16. Compatibility and migration

Existing `pair`, `monitor`, `simulate`, `validate`, and `list-profiles` concepts are retained where useful. Existing `execute` becomes a compatibility alias for `run` during migration. `monitor` remains a diagnostic command; ordinary batch execution uses integrated monitoring.

The current workload JSONL fields are accepted and wrapped by the device agent. Current CPU/GPU workload CLI arguments remain stable. The Monitor result parser changes from exact `PASS`/`FAIL` string matching to typed handling of every workload result and nonzero exit code.

## 17. Development priority and completion

The implementation followed this order. Items 1–16 are present in the repository and offline coverage for item 17 is present; hardware-in-the-loop coverage remains:

1. Fix Python module names/imports and make pyserial/PyYAML required runtime dependencies. — Complete.
2. Define and test configuration, event, artifact, result, fingerprint, and baseline schemas.
3. Implement packaged-safe `PathResolver`; remove global working-directory changes.
4. Implement real CPU/GPU profiles and schema validation.
5. Refactor typed transport results and idempotent deployment with hashes.
6. Implement platform probing and normalized capability output.
7. Implement the single-writer device agent and UART event protocol.
8. Replace regex parsing with incremental JSONL decoding and integrity checks.
9. Implement CPU and GPU golden services.
10. Implement calibration statistics and environmental sample rejection.
11. Implement baseline registry, approval, immutability, invalidation, and compatibility resolution.
12. Implement integrated production `run`, policy evaluation, cleanup, and state restoration.
13. Implement telemetry adapters and critical-only kernel filtering.
14. Implement GPU performance limits or equivalent PC policy.
15. Implement artifact collection, reports, and drift/reference-board workflows.
16. Add PyInstaller specification, bundled resources, dependency hooks, and packaged-path tests.
17. Add unit, protocol, simulation, failure-injection, packaging, and hardware-in-the-loop regression suites. — Offline suites complete; package-build and hardware suites pending their environments.

## 18. Testing strategy

### 18.1 Unit tests

- Path precedence in source, one-folder, and one-file packaging modes.
- Profile/schema validation and override protection.
- Fingerprint stability and invalidation.
- Threshold calculations and margin policies.
- Event decoding under fragmentation, concatenation, invalid UTF-8, malformed JSON, duplicate/out-of-order/missing sequence numbers, and CRC failure.
- Verdict priority and workload result mapping.

### 18.2 Integration tests

- Mock ADB/HDC connect/invoke/push/pull/hash.
- Idempotent deployment.
- Agent lifecycle and guaranteed restoration.
- CPU and GPU golden artifact handoff.
- Calibration acceptance/rejection.
- Baseline approval and batch resolution.
- Serial disconnect, workload crash, missing summary, agent crash, and user abort.

### 18.3 Hardware-in-the-loop tests

- Single- and dual-framework interface equivalence.
- Known-good PASS profiles.
- Deliberate CPU checksum and performance failures.
- Deliberate GPU golden mismatch and timeout.
- Frequency, temperature, throttle, and offline-core violations.
- Filtered kernel critical events without UART flooding.
- Recovery after interrupted execution.

## 19. Acceptance criteria

The refactoring is complete when:

- A packaged executable runs without source-tree assumptions.
- All public commands and schemas in the user guide are implemented and versioned.
- One approved CPU and one approved GPU baseline can be produced on known-good boards.
- Batch runs reuse those baselines without recalibration.
- Only one device process writes framed JSONL to UART.
- Serial corruption yields an infrastructure error, never a false DUT verdict.
- Every workload terminal result and exit code is preserved.
- Required telemetry is collected with normalized units and interface provenance.
- Raw `dmesg` is excluded from UART while critical kernel evidence is retained.
- Device state is restored after pass, failure, timeout, abort, and transport loss.
- Every run produces a complete hashed artifact directory and machine-readable result.

## 20. Release decisions and hardware-dependent work

- Replace the current fixed Python bring-up agent with a standalone native program, or confirm `python3` is part of the production board contract.
- GPU performance and telemetry limits are currently enforced by the PC policy engine; decide whether selected limits should also be enforced inside the native workload.
- Which statistical method and margin policy are approved for each production profile.
- Where approved baselines are stored and how approval/signing identities are managed.
- Which HarmonyOS builds expose debugfs on production-like boards and which sysfs/freqdump fallbacks are mandatory.
