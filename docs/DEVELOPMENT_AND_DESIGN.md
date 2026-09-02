# Monitor Development and Design

Version 2.1 design, updated 2026-09-01.

## 1. Purpose

Monitor provides a small, observable hardware-test runtime:

1. Parse a named profile.
2. Start an already deployed device agent and workload.
3. Receive framed verdict-bearing events from a selected UART.
4. Return a typed PC-side result.
5. Retain complete append-only evidence on the device for later collection.

Qualification, deployment, platform discovery, telemetry, reporting, and future scheduling are independent capabilities. They may share libraries, but one CLI command must not silently execute another command's responsibility.

## 2. Design principles

- `run` has a short dependency chain and never prepares the device implicitly.
- A baseline is optional. Absence means error-only judgement, not an automatic baseline search.
- `probe`, `deploy`, and `verify-deployment` are explicit preparation commands.
- Monitor does not write governor, frequency, CPU-online, power-policy, or affinity state.
- The workload agent owns workload launch; the native relay is the only process that opens/writes the UART.
- Telemetry is an independent device-local collector and never floods the verdict UART.
- Device evidence is authoritative, append-only, grouped by test ID, and retained after PASS and FAIL.
- PC raw serial/event persistence is optional; the PC always retains a compact result.
- HDC/ADB commands remain short. Repeated telemetry paths are placed in a deployed data plan.

## 3. Command boundaries

| Command | Responsibility | Must not do |
|---|---|---|
| `validate` | Parse and validate local profiles, platforms, assets, and optional baselines | Contact or modify the device |
| `probe` | Discover platform identity and capabilities; save a snapshot | Deploy, run, or change policy state |
| `relay probe` | Report device ABI and test deployed relay/termios/tcdrain without payload | Deploy a relay or launch a workload |
| `pair` | Resolve device UART to PC serial port | Launch a workload |
| `deploy` | Push selected versioned assets and telemetry plans; verify hashes | Start tests |
| `verify-deployment` | Read-only remote hash verification | Push or remove files |
| `run` | Parse profile, optionally resolve baseline, launch agent/workload, judge UART | Probe, deploy, mutate environment, generate golden, delete device evidence |
| `smoke` | Deprecated compatibility alias over `run`; retained for one transition cycle | Become a second execution implementation |
| `telemetry run` | Run the deployed telemetry collector independently | Require UART or launch workload |
| `collect` | Pull one test or attempt and optionally verify hashes | Delete remote evidence unless explicitly requested after verification |
| `golden` | Capture trusted correctness references from prepared devices or supplied runs | Approve a baseline |
| `calibrate` | Consume supplied collected run directories and propose a draft baseline | Start live runs or prepare devices |
| `baseline` | Manage draft/approved/deprecated baseline lifecycle | Run hardware |
| `report`/`simulate` | Process stored evidence offline | Contact the device |

## 4. Runtime architecture

```text
PC CLI
  ├─ profile resolver
  ├─ optional baseline resolver
  ├─ UART-v2 COBS/CRC session decoder
  ├─ basic/baseline policy evaluator
  └─ compact PC artifact store
        │
        ├── HDC/ADB: short agent launch command
        │
        └── UART: agent/workload lifecycle and verdict events

Device
  ├─ avs-device-agent
  │    ├─ append agent/workload evidence
  │    ├─ launch workload
  │    ├─ retain full accepted workload JSONL locally
  │    └─ submit compact verdict events to a FIFO
  ├─ avs-uart-relay
  │    └─ frame, write-all, and tcdrain the UART
  ├─ cpu/gpu workload
  └─ avs-telemetry-agent (optional independent process)
       └─ append device-local telemetry.jsonl
```

The core agent does not contain sysfs policy writes, telemetry parsing, dmesg monitoring, golden generation policy, or deployment logic.

## 5. Runtime identity

Two identifiers are used deliberately:

- `test_id`: operator-controlled identity grouping one test campaign or test case.
- `attempt_id`: unique identity for one execution attempt.

Protocol schema v1 retains the field name `run_id`; its value is the attempt ID. New device events also carry the additive top-level `test_id` field. The PC verifies both fields for new manifests.

Device layout:

```text
/data/local/tmp/avs/tests/<test-id>/<attempt-id>/spool/
  events.jsonl
  workload.log
  workload-stderr.log
  workload-diagnostics.log
  relay.log
  telemetry.jsonl          # only when requested
  telemetry-agent.log      # only when requested
  final.json
  artifact-hashes.json
```

Every stream is opened in append mode. A repeated operator test ID creates another attempt directory, preventing truncation and sequence collision. `collect --test-id` pulls every attempt together.

## 6. Profile model

A workload profile retains:

- name, target, and platform;
- local/remote workload binary and configuration;
- workload assets such as shaders;
- optional baseline reference metadata;
- telemetry metric selection and interval;
- `scheduler_requirements` as data for a future scheduler.

Legacy `environment` remains readable for configuration compatibility, but is treated as scheduler-requirement metadata. New profiles should use `scheduler_requirements`.

Example:

```yaml
schema_version: 1
name: cpu_stress_kirin9030
target: cpu
platform: kirin9030
workload:
  binary: ../../tools/cpu-avs-workload
  remote_binary: bin/cpu-avs-workload
  config: ../workloads/cpu_stress.json
scheduler_requirements: {}
baseline: null
telemetry:
  interval_ms: 5000
  required: [cpu.frequency, cpu.temperature, cpu.utilization]
  optional: [cpu.online]
kernel_monitor: off
```

`kernel_monitor` is retained only for configuration compatibility in schema v1; core runtime does not launch dmesg collection.

## 7. Agent contract

The core agent receives only bounded scalar paths/IDs plus the workload argv:

```text
sh avs-device-agent
  --test-id TEST
  --attempt-id ATTEMPT
  --target cpu|gpu
  --uart /dev/tty...
  --spool-dir ...
  --cwd ...
  --baudrate N
  --relay PATH
  --max-frame N
  --tail-guard N
  --safe-utilization PERCENT
  --timeout N
  --telemetry-shutdown-timeout N
  [--summary-metric NAME ...]
  [--telemetry-agent PATH --telemetry-plan PATH --telemetry-interval N]
  -- WORKLOAD ARGS...
```

It emits `agent_start` before launching optional telemetry or the workload. Workload stdout, stderr, and diagnostics are separate append-only files. Full recognized JSONL is wrapped into local `events.jsonl`; UART receives only compact lifecycle/verdict events. `DEBUG:`, `TRACE:`, and `INFO:` output is diagnostic, while other malformed stdout becomes `WORKLOAD_OUTPUT_INVALID`. Baseline threshold metric names are passed explicitly so the compact summary contains only fields required for live policy. A workload that exceeds its manifest guard emits `WORKLOAD_DEADLINE_EXCEEDED`. The agent ends with `agent_final`, closes the relay FIFO, and waits for relay drain.

Required UART lifecycle for a successful attempt:

```text
agent_start
workload start/heartbeat/.../summary
agent_final
```

Transport framing is `NUL + COBS(compact JSON + CRC32-LE) + NUL`, capped by the platform `max_frame_bytes`. Before a matching `agent_start`, PC discards corrupt or stale frames. After matching START it fails closed on CRC, session identity, sequence, or framing errors. FINAL—not HDC process completion plus a fixed delay—closes success. The native ISO-C/POSIX relay supports configured standard baud rates, handles partial/EINTR writes, and calls `tcdrain()` for every bounded frame.

Some UART/DMA stacks retain a short EOF tail even after `tcdrain()` reports success. Therefore `serial.tail_guard_bytes` (default 64, valid 0–4096) is carried through the manifest and agent to the relay. At FIFO EOF the relay writes that many NUL delimiters and drains once more. This keeps the relay semantic-free: the receiver ignores empty delimiter frames, while the complete FINAL is pushed ahead of any bytes the driver may retain. PC post-agent grace includes the configured frame and guard wire time. A run whose transport worker remains active after UART evaluation is cancelled and reported as `INFRA_ERROR` rather than configuration error.

No `environment` or restoration event is required.

## 8. Judgement modes

### 8.1 Error-only

Used when `run` has no `--baseline`. PASS requires:

- valid UTF-8/JSONL/schema/test ID/attempt ID/sequence;
- workload summary with `result=PASS` and exit code 0;
- matching agent/workload process exit;
- `agent_final`;
- no workload error/violation;
- no heartbeat or overall timeout.

Telemetry presence and performance thresholds do not participate.

### 8.2 Baseline

Enabled only by explicit `--baseline ID`. It adds:

- approved/immutable baseline validation;
- profile/target/platform/fingerprint matching;
- CPU checksum or GPU golden reference argv;
- correctness and performance limits available in workload events.

Device-local telemetry can be assessed after collection during calibration/reporting. It is not streamed merely to satisfy live baseline policy.

## 9. Telemetry design

`avs-telemetry-agent` is independent of the core agent. `deploy` generates a data-only plan:

```text
# metric|parser|device-path-glob
cpu.frequency|number|/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq
cpu.utilization|proc_stat_utilization|/proc/stat
cpu.temperature|temperature_auto|/sys/class/thermal/thermal_zone*/temp
```

The collector accepts test/attempt IDs, target, output, plan, interval, duration, and optional stop-file. It writes only device-local JSONL. It can run:

- standalone through `telemetry run`;
- alongside workload through `run --telemetry`.

Telemetry failure is recorded locally and in `agent_final.telemetry_exit_code`. Telemetry is not required unless explicitly requested; an explicitly requested collector that produces zero samples is an infrastructure failure. The collector does not depend on `awk` or `tr`. It checks stop-file and wall-clock deadline before and after every path read and uses interruptible interval waits. The core agent applies the manifest `telemetry.shutdown_timeout_s` as a bounded grace period, then terminates a stuck collector and records `telemetry_timed_out=true`; this failure never suppresses `final.json` or UART FINAL.

## 10. Future scheduler boundary

The following operations are intentionally not implemented by Monitor runtime:

- set/read back/restore CPU or GPU governor;
- set/read back/restore min/max frequency;
- online/offline CPU cores;
- process affinity/taskset;
- power-policy changes;
- conflict arbitration with other device services.

A future scheduler module must own the complete apply/readback/rollback transaction. Its interface should provide:

1. requested state and ownership token;
2. pre-change snapshot;
3. per-path apply/readback result;
4. conflict and permission reporting;
5. guaranteed rollback and rollback evidence;
6. an immutable environment snapshot referenced by qualification artifacts.

Monitor may record scheduler requirements and a supplied scheduler snapshot, but must not partially implement those writes itself.

## 11. Preparation and packaging

PyInstaller packages the complete `device` directory, configuration, documentation, staged workload assets, and a staged relay binary. The relay is built from `native/uart_relay/avs_uart_relay.c` with the same OpenHarmony target/sysroot/ABI as workloads; no ABI is guessed by Python. `relay probe --platform ...` reports `uname -m`/word size and, after deployment, separately tests version, self-test, and an optional zero-payload UART open/configure/drain. At runtime resources are extracted from `_MEI...`; `deploy` hashes and pushes selected files to stable device paths.

`deploy` also generates and deploys the telemetry plan for each selected profile. `verify-deployment` regenerates the same deterministic local plan and compares every selected remote hash without writing to the device.

Neither command is called by `run`.

The public CLI has one workload execution command: `run`. The former `execute` command is removed. Short tests select a smoke profile through `run`; the deprecated `smoke` alias remains only for one compatibility cycle. Connection settings use the global `--transport`, `--device`, `--pc-serial`, `--device-uart`, and `--baudrate` options instead of command-local synonyms.

## 12. Evidence and collection

Device evidence is retained after every verdict. `collect` defaults to preserving remote evidence. Remote removal requires all of:

1. `--verify-hashes`;
2. successful verification of every discovered hash manifest;
3. explicit `--remove-remote-after-verify`;
4. a path strictly below `/data/local/tmp/avs/tests`.

PC artifact levels:

- `result`: run manifest, effective-profile fingerprint, workload summary, result, and hashes;
- `full`: everything in `result` plus normalized events and raw serial.

## 13. Qualification lifecycle

```text
explicit validate/probe/pair/deploy/verify
  -> golden (all-live capture OR an exact supplied cohort)
  -> complete attempt evidence normalized on the PC
  -> calibrate from an exact explicit multi-board cohort
  -> review draft
  -> baseline approve
  -> baseline-aware deploy/verify/run
```

`golden --runs N` accepts zero `--run-dir` values or exactly N values. Zero means live capture on an already prepared known-good device. Live capture enables the existing device-local telemetry collector, runs the qualification workload, pulls the complete device attempt directory, preserves the native full workload summary, and reports reusable PC directories in `source_runs`. A partial cohort is a configuration error; it is never completed from hardware implicitly.

For live capture, the public `qualification_id` is also the device/PC `test_id`; each attempt appends a unique suffix. This makes the IDs returned by a failed command directly usable with `collect`. Existing CPU and GPU golden artifacts are fail-closed and are not overwritten by reusing a qualification ID.

Qualification liveness has four distinct bounds. Let `T` be the validated workload JSON `timeout`: the device workload guard is `T + 5s`; the pre-summary heartbeat window is `max(default heartbeat, guard + 10s)`; after a valid workload summary, FINAL has a separate 20-second window; overall remains 300 seconds. For the current CPU profile (`T=75`) these are 80/90/20/300 seconds. Normal `run` retains its strict 45-second heartbeat window. This accommodates the workload's current synchronous golden phase without treating execution as unbounded. The preferred workload-side contract is still to emit heartbeats throughout golden computation and enforce its own deadline.

If UART evaluation has already produced a verdict, cancelling a still-running HDC/ADB worker is appended as secondary infrastructure evidence instead of replacing the verdict with a transport exception. A failed live qualification returns structured `test_id`, `attempt_id`, `result_path`, remote/local evidence locations, verdict, and DUT/infrastructure reasons after a best-effort evidence pull.

The normalization layer accepts any of these inputs:

```text
PC attempt/
  result.json
  device-evidence/<attempt>/spool/{events.jsonl,workload.log,telemetry.jsonl,...}

collected attempt/spool/
  events.jsonl
  workload.log
  telemetry.jsonl
```

When a spool is supplied, the resolver pairs it with the standard sibling PC attempt when available. The last native `type=summary` record in device `workload.log` takes precedence over the compact PC `workload-summary.json`; this preserves qualification metrics without enlarging verdict UART frames. CPU calibration consumes `operations_per_sec_avg` and `batch_time_ms_p99`; GPU consumes `fps_avg` and `frame_time_p99_ms`.

`calibrate` is always offline and requires exactly `--runs` normalized inputs. The default production policy rejects telemetry gaps, throttling, temperature-range violations, missing metrics, fewer than 20 accepted samples, or fewer than two boards. `--min-accepted 2` exists only for a two-board functional data-chain acceptance test; it does not redefine production policy. Calibration creates an immutable-registry draft, never an approved baseline.

Kirin9030 uses dedicated `cpu_qualification_kirin9030` and `gpu_qualification_kirin9030` profiles. Platform identity and workload/correctness fingerprints are baseline compatibility fields; qualification artifacts from a Kirin9020 profile cannot be relabeled as Kirin9030 evidence.

## 14. UART-v2 diagnostic interfaces

Core `run`, `simulate --raw-serial`, and `monitor` share the UART-v2 framing implementation. A diagnostic session decoder scans only until a valid matching START is discovered, discards stale/corrupt preamble frames, and then delegates to the fail-closed `UartV2Decoder` for CRC, frame-size, schema, identity, sequence, FINAL, and trailing-data checks.

`simulate --events` remains a separate JSONL replay path and may use `--realtime`. `simulate --raw-serial` deterministically replays a stored NUL-delimited COBS+CRC capture and rejects `--realtime`; it builds the same serial-transport manifest expected by `RunOrchestrator`. Every replay writes below a unique `output/simulations/<replay-id>/<original-test-id>/<original-run-id>` namespace and records source path/hash. Original identities remain unchanged for protocol evaluation, while live artifacts are immutable. `monitor` performs live session discovery and artifact capture but deliberately returns `NOT_EVALUATED`: without profile policy, workload process status, and a complete run manifest it is a protocol diagnostic, not a DUT judge.

Neither interface translates UART v2 through `awk`, `tr`, newline framing, or platform shell helpers. They operate on bytes on the PC, so HarmonyOS portability remains confined to the native relay and device agent.

## 15. Acceptance criteria

The refactor is complete when:

- `run` contains no call to probe or deployment services;
- baseline omission reaches agent launch in error-only mode;
- no runtime code or agent writes governor/frequency/online/affinity paths;
- agent launch argv contains no repeated telemetry paths or kernel regexes;
- smoke adds no golden-generation argument;
- standalone and alongside telemetry use the same deployed collector/plan;
- device events/logs are append-only under test/attempt IDs;
- PASS never deletes device evidence;
- PC result/full artifact modes both judge correctly;
- collect retains remote evidence by default;
- golden live capture returns complete reusable source directories, while partial supplied cohorts fail;
- calibration reads device-native full metrics and enforces multi-board policy;
- raw simulation and monitor decode the same UART-v2 framing as `run`;
- unit, shell, protocol, and packaging validation pass.
