# NPU 软件验证与板端待验记录

记录日期：2026年10月26日。此文件将前一阶段的软件结果中文化；后续代码统一和集中构建的验证见 [交接验证记录](zh/交接验证记录.md)。

## 接入阶段基线与结果

本阶段为 NPU 接入后的主机软件检查，后续公共接口和集中构建检查见中文交接验证记录。

| 软件检查 | 实际结果 |
|---|---|
| 原 Monitor Windows 基线 | 148 项，跳过 9 项 POSIX 测试，其余通过 |
| 原 Monitor Linux 基线 | 148 项全部通过 |
| NPU 接入后 Monitor Linux | 172 项全部通过 |
| NPU 接入后 Monitor Windows | 172 项，跳过 12 项 POSIX 测试，其余通过 |
| NPU 原生协议测试 | Windows/Linux 各 11 项通过，含四模型 fake delegate |
| 既有 Windows NPU smoke | 13 个用例通过，包含预期错误退出 |
| manifest/tensor 原生契约 | 五个 manifest 通过，含 framework 夹具 |
| 编译 | 主机 Windows g++ 和 WSL/Linux g++；无 Android/OHOS NDK |
| 静态检查 | Python 编译、PowerShell 语法和 文件空白检查通过 |

原始日志保留于 docs/validation，不翻译 JSON 字段、错误码或原始文本。新集中入口补充统一源码的产物 SHA 与构建日志。主机测试不能当作板端压测记录。

## 已覆盖的软件边界

四模型的 manifest/输入/模型哈希和张量签名，路径越界和非法类型，真实校验计数、成功日志节流、错误 golden、失败后无 summary、长摘要有界转发、CRC/序号/身份、原始 UART 与事件重放、PC/native/FINAL 一致性、外部审批参考及厂商证据绑定，均有对应测试。模拟 cohort 证明校准数据链路接受/拒绝规则，不证明真实两板性能。

## 板端尚未完成

- 用真实 Android/OHOS SDK 交叉编译、核实设备 ABI 和动态加载依赖。
- 在实际板卡核对 HDC/ADB、串口、波特率、relay 和部署哈希。
- 用厂商 runtime/delegate 执行四类真实模型，并取得真实执行位置诊断。
- 核实真实 NPU 温度、频率、利用率/功率接口和采样质量。
- 在独立可信条件下生成 golden，做真实持续多板样本及审批。
- 实机测试错误参考、库缺失、部署哈希错误、驱动/推理卡死、取消与清理、串口截断。
- 完成实际用例运行、日志留存和压测汇总。

null/fake-runtime 是框架证据。尚无板端 NPU、完整算子放置、降压 Vmin、功耗收益或真实板端压测完成的结论。
