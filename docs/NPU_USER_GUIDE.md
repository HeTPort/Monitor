# NPU integration user guide

NPU uses the existing `validate`, `probe`, `deploy`, `verify-deployment`, `run`, `collect`, `golden npu`, `calibrate npu`, `baseline`, `simulate`, and `report` commands. CPU/GPU profiles and their defaults are retained. `deploy --target all` retains its historical CPU/GPU selection; select NPU explicitly with `--target npu` or `--profile`.

## Compatible source pair

- Monitor: this branch is based on `74bc567` from `codex/verify-contract-profiles`.
- Workload: use the paired `codex/npu-monitor-protocol` branch based on `ccf431f` (NPU version 0.4.0, verification contract 2).
- The old CPU/GPU verification branch was inspected for the scheduling/rate-limit contract. It was not copied over the newer workload mainline. This change does not modify either CPU or GPU C++ sources.
- The working copies are `D:\Monitor-main\npu-monitor` and `D:\Monitor-main\npu-workload`. The original repositories and September worktrees are preserved.

## Profiles and prerequisites

Each of `kws01`, `ic01`, `ad01`, and `sww01` has three Monitor profiles:

| Suffix | Backend | Meaning |
|---|---|---|
| `_smoke` | `null` | Device deployment, manifest/tensor checking, events and collection. No NPU execution claim. |
| `_load` | `tflite_delegate` | Repeated real delegate inference. Without approved golden: run consistency only; placement unknown. |
| `_qualification` | `tflite_delegate` | Exact external golden, per-inference verification, vendor placement evidence and qualification sample gates. |

Example names: `npu_kws01_smoke`, `npu_kws01_load`, `npu_kws01_qualification`. Templates use the existing Kirin9030 transport settings. No NPU sysfs paths are guessed. Validate and adapt the platform before hardware use.

Build the NPU executable for the device with the workload project's OHOS/Android toolchain. From this Monitor directory, stage the explicit AArch64 ELF executable and checked manifests/models/inputs:

```powershell
python .\scripts\stage_npu_assets.py --workload-root D:\Monitor-main\npu-workload --binary D:\artifacts\ohos\npu-avs-workload
```

This preserves MLCommons provenance and license files. Host EXEs/DLLs are rejected by device profiles. The stage operation verifies source asset hashes before copying. CPU/GPU executable/shader staging remains as before. Build the relay separately for the device ABI.

For delegate profiles supply `tools/npu/lib/libtensorflowlite_c.so`, `tools/npu/lib/libvendor_delegate.so`, and any dependent libraries. Paths in workload JSON resolve relative to that JSON. Managed runtime/delegate objects are checked as AArch64 ELF, copied under the model profile's `lib/`, and SHA-256 verified. Add managed transitive dependencies to `workload.assets`, with `kind: npu-dependency`, and a remote path matching the vendor loader's requirements. Do not ship proprietary libraries without the appropriate distribution rights.

Alternatively set `workload.installed_libraries` in the Monitor profile:

```json
{
  "runtime_library": {"path": "/vendor/lib64/libtensorflowlite_c.so", "sha256": "<64 lowercase hex>"},
  "delegate_library": {"path": "/vendor/lib64/libvendor_delegate.so", "sha256": "<64 lowercase hex>"}
}
```

The native workload config must use these same absolute paths. `verify-deployment` and `run` verify each installed SHA-256 and pull a read-only content-addressed copy once to check the actual ELF architecture. Vendor dynamic-loader compatibility and permissions are finally exercised by real backend initialization. A load/initialization failure remains an infrastructure error, not proof of a calculation defect.

## Device smoke and load

```powershell
python .\main.py validate --profile npu_kws01_smoke
python .\main.py --transport hdc --device DEVICE_ID probe --platform kirin9030
python .\main.py --transport hdc --device DEVICE_ID deploy --profile npu_kws01_smoke
python .\main.py --transport hdc --device DEVICE_ID verify-deployment --profile npu_kws01_smoke
python .\main.py --transport hdc --device DEVICE_ID --pc-serial COM4 run --profile npu_kws01_smoke --test-id NPU-SMOKE --pc-artifacts full
```

Use the attempt ID printed by `run` with the existing `collect --test-id ... --attempt-id ...` interface. Run `collect --help` for the selected/full evidence modes. `run` only verifies existing deployment; it never silently repairs or deploys it.

After staging vendor libraries, repeat the same sequence for `npu_kws01_load`, then IC01, AD01, and SWW01. A null/fake backend PASS certifies the framework path only. A delegate PASS without independent placement evidence does not certify complete NPU placement.

Deployment materializes a content-addressed effective JSON under `output/effective-configs/`. Its manifest/runtime paths are device paths; the user's source JSON stays unchanged. All model/input paths remain relative to the manifest's preserved directory structure. Missing files, path escapes, mismatched tensor descriptors, and bad declared hashes are rejected before deployment. The deployment manifest records every managed file identity.

## Native parameter semantics

`duration` is measured seconds; `inferences` is a count and either positive stop condition can end the measured phase. `warmup_inferences` is a count, never seconds. `timeout` is the native total budget including preparation/warm-up; `inference_timeout_ms` bounds waiting for one submitted inference. The device agent adds an independent process watchdog. `runtime_budgets.overall_timeout_s` and `heartbeat_timeout_s` change only NPU profiles and should cover the device's actual initialization and inference budget. Cancel/cleanup remains bounded by existing agent and transport behavior.

`verify_interval` selects actual checks on completed measured inference multiples. `verify_count` is incremented only at that check path. A normal successful run has `verify_count == inference_count // verify_interval`; warm-up and golden generation contribute zero to both measured counts. `success_log_interval` selects the first and every Nth subsequent successful check (0 disables success events). An additional shared one-second rate limit bounds success logs. Failed checks always emit immediately, including when success logs or ordinary events are suppressed. Heartbeats are emitted after actual warm-up/inference progress; no background thread invents progress.

JSON must use canonical underscore spellings with native values. Monitor rejects aliases, unsupported keys, booleans in numeric fields, nonfinite values and unsafe path delimiters. Native config parsing is strict JSON, detects duplicate keys and rejects conflicting aliases. Live Monitor profiles require JSONL, positive heartbeat cadence, and `summary_only=false`. The native standalone `summary_only` option remains usable for offline work.

## Vendor placement evidence

`fallback_used=false` is only a runner fact. Full placement requires vendor diagnostics interpreted by an operator. Save a JSON file and set its path in `workload.placement_evidence` (relative to the Monitor profile, or absolute). Required fields:

```json
{
  "schema_version": 1,
  "execution": "full_npu",
  "vendor": "<vendor>",
  "tool": "<vendor tool and version>",
  "captured_at": "<timestamp>",
  "diagnostic_file": "vendor-placement.txt",
  "diagnostic_sha256": "<SHA-256 of that diagnostic>",
  "asset_sha256": {"<absolute deployed asset path>": "<SHA-256>"}
}
```

`asset_sha256` must exactly match the selected workload executable, effective config, manifest, model, inputs and managed runtime/delegate/dependency assets, plus any installed pinned libraries. It excludes the Monitor agent, relay and telemetry helpers. Use the verified deployment manifest to obtain the file identities; verify the vendor diagnostic actually proves full placement for those assets before writing `execution: full_npu`. This is an operator/vendor attestation with content hashes, not an automatic vendor-log parser or a cryptographic signature. A changed model, input, config, runtime, delegate or binary invalidates the binding. Unknown/partial placement cannot pass the qualification gate.

## Known-good golden and calibration

Use the **same qualification profile and asset identity** for known-good golden generation and later sustained captures. Changing profile, assets or placement evidence requires generating a new reference. Acquire the reference only at trusted known-good operating conditions; the tool does not set voltage/frequency.

1. Add and verify the placement evidence and device dependencies, then deploy/verify the qualification profile.
2. On the known-good board run:

```powershell
python .\main.py --transport hdc --device DEVICE_ID --pc-serial COM4 golden npu --profile npu_kws01_qualification --runs 3 --board-id BOARD_A --known-good --approver OPERATOR --conditions "nominal OPP; recorded temperature; vendor diagnostic reference" --qualification-id NPU-KWS-GOLDEN
```

`golden npu` also accepts exactly `--runs` supplied `--run-dir` directories using the existing normalized collected-run layout. It requires paired PC context and hash-verified device `events.jsonl`, native summary and FINAL, matching identities, real delegate backend and bound vendor evidence. Golden-generation summary records have zero measured inferences/checks. Checksums must agree across repeats. `--accept-checksum` optionally pins an independently expected checksum. The resulting `npu-golden.json` includes explicit known-good acknowledgement, approver, generation conditions and all correctness identities. This acknowledgement is an operator assertion; the program cannot verify physical OPP conditions on its own.

3. For sustained captures use ordinary `run --profile npu_kws01_qualification --golden PATH_TO_NPU_GOLDEN --telemetry --pc-artifacts full`, then collect the complete device evidence. Use the same commands on at least two boards. Formal samples require exact per-inference checks; zero/contradictory counts, missing summary/FINAL, native/PC disagreement, unknown placement, hash mismatch and framework backends are rejected.
4. Declare actual required NPU telemetry interfaces in the platform/profile. Required temperature/frequency/utilization paths must be measured on the target; optional unavailable metrics remain unavailable. Never substitute CPU/GPU readings. The existing calibration defaults require temperature in range, adequate telemetry/duration coverage, at least 20 accepted samples and two boards. Leave these production defaults intact.
5. Run offline `calibrate npu --help` for the shared interface. Example functional data-chain check (two accepted samples is not production qualification):

```powershell
python .\main.py calibrate npu --profile npu_kws01_qualification --golden PATH_TO_NPU_GOLDEN --runs 2 --board-id BOARD_A --min-accepted 2 --run-dir BOARD_A=PATH_A --run-dir BOARD_B=PATH_B --baseline-id npu-kws-draft
```

The result is a draft baseline. Use the existing explicit baseline approval workflow before production use. Performance thresholds use `inferences_per_sec_avg` and `host_inference_time_ms_p99`; they never mix units with CPU operations/s or GPU frames/s. Device latency is available only when `device_time_valid=true`; unknown values are not replaced by zero in normalized reports. Operation count is retained as native evidence, never inferred as NPU hardware operations.

## Evidence, replay, reporting and packaging

Full device logs retain native inference events and full partition/model diagnostics. Only bounded verification/performance fields and the first failing verify travel over UART, with the existing v2 identity, sequencing, CRC and FINAL rules. Split verification fields are merged with contradiction checks. Both raw-serial and framed-event replay use the common decoders. Reports include normalized units, correctness scope, and independently bound placement status.

CPU/GPU-only packaging remains available with `scripts/build.ps1` and `validate --package`. To require NPU assets explicitly use `scripts/build.ps1 -IncludeNpu` or `validate --package --include-npu`. Proprietary runtime libraries and actual device binaries are not fabricated or committed as placeholders.

See [NPU_VALIDATION.md](NPU_VALIDATION.md) for executed software checks and outstanding hardware acceptance.
