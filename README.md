# Vmin Judge Monitor

The Monitor qualifies reusable CPU/GPU correctness and performance baselines, then executes batch tests against approved immutable baseline versions. It monitors framed workload, telemetry, and critical kernel evidence over one serial stream.

Start with:

- [Development and design](docs/DEVELOPMENT_AND_DESIGN.md)
- [User guide and API reference](docs/USER_GUIDE.md)
- [Packaged release and device acceptance test plan](docs/PACKAGE_AND_DEVICE_TEST_PLAN.md)

Run the offline regression suite:

```powershell
python -m unittest discover -s tests -v
```

Build the packaged executable after installing `requirements-dev.txt`:

```powershell
.\scripts\build.ps1
```

Before packaging, stage the HarmonyOS workload binaries at `tools\cpu-avs-workload` and `tools\gpu-avs-workload`, and compile/stage the Vulkan shaders at `tools\shaders\vulkan\fullscreen.vert.spv` and `tools\shaders\vulkan\workload.frag.spv`. The build script fails closed when any release asset is missing.

No active execution path clears the kernel ring buffer or transmits raw `dmesg`. The device agent sends only configured warning/critical matches and optionally retains raw kernel output in its device-local spool.
