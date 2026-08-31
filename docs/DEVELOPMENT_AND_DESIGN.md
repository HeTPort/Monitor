# Monitor Development and Design

Version 2.1 design, updated 2026-08-31.

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
- The workload agent owns workload launch and is the only UART writer.
- Telemetry is an independent device-local collector and never floods the verdict UART.
- Device evidence is authoritative, append-only, grouped by test ID, and retained after PASS and FAIL.
- PC raw serial/event persistence is optional; the PC always retains a compact result.
- HDC/ADB commands remain short. Repeated telemetry paths are placed in a deployed data plan.

## 3. Command boundaries

| Command | Responsibility | Must not do |
|---|---|---|
| `validate` | Parse and validate local profiles, platforms, assets, and optional baselines | Contact or modify the device |
| `probe` | Discover platform identity and capabilities; save a snapshot | Deploy, run, or change policy state |
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
  ├─ UART event decoder
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
  │    ├─ frame accepted workload JSONL
  │    └─ sole UART writer
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
  --timeout N
  [--telemetry-agent PATH --telemetry-plan PATH --telemetry-interval N]
  -- WORKLOAD ARGS...
```

It emits `agent_start` before launching optional telemetry or the workload. Workload stdout/stderr is appended to `workload.log`; recognized JSONL types are wrapped and sent to both `events.jsonl` and UART. Unrecognized lines become `WORKLOAD_OUTPUT_INVALID`. The agent ends with `agent_final` and a local `final.json`.

Required UART lifecycle for a successful attempt:

```text
agent_start
workload start/heartbeat/.../summary
agent_final
```

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

Telemetry failure is recorded locally and in `agent_final.telemetry_exit_code`; ordinary error-only workload judgement does not depend on telemetry.

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

PyInstaller packages the complete `device` directory, configuration, documentation, and staged workload assets. At runtime resources are extracted from `_MEI...`; `deploy` hashes and pushes selected files to stable device paths.

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
  -> golden capture on known-good prepared devices
  -> ordinary/baseline runs with retained device evidence
  -> collect by test ID
  -> calibrate from explicit run directories
  -> review draft
  -> baseline approve
```

`calibrate` never fills missing samples by launching hardware. Missing `--run-dir` samples are an error.

## 14. Acceptance criteria

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
- unit, shell, protocol, and packaging validation pass.
