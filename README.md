# Monitor

Monitor 2.1 是一个面向 CPU/GPU 板级压力测试的最小运行与判定工具。核心 `run` 只做四件事：启动已部署的设备 agent、启动 workload、把判定事件定向输出到 UART、由 PC 侧解析并给出 PASS/FAIL。

准备动作是独立命令：

```powershell
python main.py validate --package
python main.py --transport hdc --device <DEVICE> probe --platform kirin9030 --full
python main.py --transport hdc --device <DEVICE> deploy --profile cpu_stress_kirin9030
python main.py --transport hdc --device <DEVICE> verify-deployment --profile cpu_stress_kirin9030
```

无 baseline 的最小运行：

```powershell
python main.py --transport hdc --device <DEVICE> --pc-serial COM4 --device-uart /dev/ttyHW0 run --profile cpu_stress_kirin9030 --test-id TEST-001
```

baseline 只在需要 checksum、golden 或阈值校验时通过 `--baseline` 显式启用。Telemetry 可以通过 `telemetry run` 独立采样，或在核心测试中用 `run --telemetry` 伴随启动；采样日志只追加到设备本地。所有设备证据按 `test_id/attempt_id` 保留，之后用 `collect --test-id ...` 一次性拉取。

Monitor 不修改或恢复 governor、频率、CPU online、功耗策略和 affinity；这些属于未来独立调度模块。

文档：

- [开发与设计](docs/DEVELOPMENT_AND_DESIGN.md)
- [用户指南](docs/USER_GUIDE.md)
- [打包与设备最小闭环测试计划](docs/PACKAGE_AND_DEVICE_TEST_PLAN.md)

离线回归：

```powershell
python -m unittest discover -s tests -v
```

打包前必须把真实板端 workload 放在 `tools\cpu-avs-workload`、`tools\gpu-avs-workload`，GPU shader 放在 `tools\shaders\vulkan\`。构建：

```powershell
.\scripts\build.ps1
```
