# NPU 使用与证据指南

NPU 沿用 Monitor 的 validate、probe、deploy、verify-deployment、run、collect、golden npu、calibrate npu、baseline、simulate 和 report，不另外复制一套 CLI。完整交接流程见 [中文使用指南](zh/使用指南.md) 和 [压测与日志交接](zh/压测与日志交接.md)。

## 用例与依赖

KWS01、IC01、AD01、SWW01 各有 smoke、load、qualification 三类 profile。smoke 使用 null，只验证模型/输入/张量资产与程序链路；load 使用 tflite_delegate 重复推理；qualification 要求可信外部 golden、实际逐次校验及厂商完整执行位置证据。

NPU workload 需要对应 SDK 编出的 AArch64 ELF。主机 `.exe`、WSL ELF 和 fake-runtime DLL/SO 不能当作设备 NPU 运行产物。集中构建见 [编译指南](zh/编译指南.md)，NPU 编译不链接厂商库，但板端必须有 TFLite C runtime、厂商 delegate 和其依赖。

```powershell
python scripts/stage_npu_assets.py --workload-root D:\Monitor-main\npu-workload --binary D:\artifacts\ohos\npu-avs-workload
```

此命令保留四模型的 manifest/model/input 相对结构和来源/许可信息。受管理库默认放 `tools/npu/lib/`，由 profile JSON 指定，部署前检查 AArch64 ELF 和 SHA。其他受管理依赖通过 workload.assets 声明 `kind: npu-dependency`。

已安装库可在 workload.installed_libraries 指定，原生 workload JSON 同时采用相同绝对路径：

```json
{
  "runtime_library": {"path": "/vendor/lib64/libtensorflowlite_c.so", "sha256": "填写64位小写十六进制"},
  "delegate_library": {"path": "/vendor/lib64/libvendor_delegate.so", "sha256": "填写64位小写十六进制"}
}
```

verify-deployment/run 检查安装库哈希，并按内容缓存只读副本核对 ELF；真实动态加载兼容性仍需板端初始化验证。缺依赖/初始化错误归入基础设施信息，不能据此认定芯片计算错误。

## 参数和计数

NPU 的 duration 是正式测量秒数，inferences 是完成次数；有多个停止条件时任一达到即可结束。warmup_inferences 是次数。timeout 为原生总体预算，inference_timeout_ms 为单次等待预算。profile.runtime_budgets 可设置 NPU 的主机总体/心跳预算，不改变 CPU/GPU 默认值。

verify_interval 选择实际校验点，summary 中 verify_count 仅在校验路径累计；正常完成应等于 inference_count 整除 verify_interval。预热与 golden 生成计数均不属于正式测量。success_log_interval 为成功检查次数采样，0 关闭成功日志，另有一秒上限；失败不被节流隐藏。心跳来自真实进展。

Monitor JSON 使用下划线字段，拒绝别名冲突、非法类型、非有限值和危险路径。实时运行必须 JSONL、有效心跳、summary_only=false。主机延迟与设备延迟分开；device_time_valid=false 时设备耗时不可用，不能填零。

## 真实负载最小闭环

下例 `$MON` 为打包后的 `vmin_judge.exe` 路径；先沿用公共指南完成 probe/pair，再执行以下部署核验步骤。

```powershell
& $MON validate --profile npu_kws01_smoke
& $MON --transport hdc --device DEVICE_ID deploy --profile npu_kws01_smoke
& $MON --transport hdc --device DEVICE_ID verify-deployment --profile npu_kws01_smoke
& $MON --transport hdc --device DEVICE_ID --pc-serial COM4 run --profile npu_kws01_smoke --test-id NPU_SMOKE --attempt-id A001 --pc-artifacts full
& $MON --transport hdc --device DEVICE_ID collect --test-id NPU_SMOKE --attempt-id A001 --verify-hashes
```

确认 framework 链路后，为 kws01_load 准备实际运行库并重复部署/核验/运行，之后扩展其余模型。原始 inference/partition 日志留设备，UART 只转发有界必要字段。没有外部 golden 的结果属于运行内一致性；没有厂商执行证据，placement 为 unknown。

## 厂商执行位置证据

fallback_used=false 只表示 runner 未主动回退，不证明全部算子在 NPU。qualification 的 workload.placement_evidence 必须指向厂商诊断绑定文件：

```json
{
  "schema_version": 1,
  "execution": "full_npu",
  "vendor": "填写厂商",
  "tool": "填写诊断工具身份",
  "captured_at": "填写实际采集时间",
  "diagnostic_file": "vendor-placement.txt",
  "diagnostic_sha256": "填写诊断文件SHA256",
  "asset_sha256": {"绝对设备资产路径": "对应SHA256"}
}
```

asset_sha256 必须与 workload、有效配置、manifest、模型、输入及运行库/依赖的完整身份一致，不含 agent/relay/telemetry 辅助文件。使用已核验的部署清单取得这些值，操作者必须确认诊断内容确实支持 full_npu。哈希绑定的是操作者/厂商证据，不是自动解析器或数字签名。资产变化需重做绑定。

## 可信 golden 与校准

使用同一 qualification profile 在独立可信条件下生成参考，填写生成条件和审批者：

```powershell
& $MON --transport hdc --device DEVICE_ID --pc-serial COM4 golden npu --profile npu_kws01_qualification --runs 3 --board-id BOARD_A --known-good --approver OPERATOR --conditions "实际已知正常运行条件与厂商诊断引用" --qualification-id NPU_KWS_GOLDEN
```

golden npu 也接受恰好 --runs 份 `--run-dir`，要求配对 PC 上下文、设备 events/summary/FINAL 及其哈希和身份一致。原生模型须匹配、backend 须为真实 delegate、厂商位置证据须绑定；生成记录 inference_count/verify_count 为零。多次 checksum 必须一致，`--accept-checksum` 可进一步固定独立期望值。

后续用相同 profile 的 `run --golden PATH --telemetry --pc-artifacts full` 采集持续样本，再用 `calibrate npu` 离线处理。正式样本要求有效持续测量、真实计数、完整遥测、至少两板及生产策略要求的样本数；默认至少 20 个接受样本。`--min-accepted 2` 仅用于两板功能链路验证。结果为 draft，通过现有 baseline approve 显式批准后才用于正式性能限值。

当前用户没有调压接口，可以先做 load 的运行记录和正确性准备；不要把尚未满足的资格化条件写为通过。没有真实 NPU 遥测路径时按 required/optional 说明缺口，不用 CPU/GPU 指标替代。
