from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config_loader import PlatformConfig
from src.platform_probe import MappingProbeBackend, PlatformProbe, ProbeError


class PlatformProbeTests(unittest.TestCase):
    def platform(
        self,
        root: Path,
        *,
        temperature_unit: str = "millidegree_celsius",
        identity: dict | None = None,
        cpu_governor: dict | None = None,
    ) -> PlatformConfig:
        path = root / "platform.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "test-soc",
                    "identity": identity or {},
                    "transport": {},
                    "serial": {},
                    "cpu": {
                        "topology_glob": "/sys/devices/system/cpu/cpu[0-9]*",
                        "interfaces": {
                            "frequency": {
                                "candidates": ["/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"],
                                "unit": "kHz",
                                "required": True,
                            },
                            "online": {
                                "candidates": ["/sys/devices/system/cpu/cpu*/online"],
                                "required": True,
                            },
                            **({"governor": cpu_governor} if cpu_governor else {}),
                        },
                    },
                    "gpu": {
                        "interfaces": {
                            "frequency": {
                                "candidates": ["/sys/class/devfreq/gpu/cur_freq"],
                                "unit": "Hz",
                                "required": True,
                            }
                        }
                    },
                    "thermal": {
                        "zone_glob": "/sys/class/thermal/thermal_zone*",
                        "temperature_unit": temperature_unit,
                        "cpu_type_patterns": ["cpu"],
                        "gpu_type_patterns": ["gpu"],
                    },
                }
            ),
            encoding="utf-8",
        )
        return PlatformConfig.from_file(path)

    def test_discovers_every_core_and_thermal_by_type(self) -> None:
        files = {
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "2100000\n",
            "/sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq": "2200000\n",
            "/sys/devices/system/cpu/cpu1/online": "1\n",
            "/sys/class/devfreq/gpu/cur_freq": "700000000\n",
            "/sys/class/thermal/thermal_zone2/type": "cpu-cluster\n",
            "/sys/class/thermal/thermal_zone2/temp": "45000\n",
            "/sys/class/thermal/thermal_zone8/type": "gpu\n",
            "/sys/class/thermal/thermal_zone8/temp": "47000\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            probe = PlatformProbe(self.platform(Path(tmp)), MappingProbeBackend(files))
            result = probe.probe(full=True)
        self.assertTrue(result["supported"])
        self.assertEqual(result["cpu_topology"]["core_count"], 2)
        self.assertEqual(len(result["capabilities"]["cpu.frequency"]["paths"]), 2)
        self.assertEqual(result["capabilities"]["cpu.temperature"]["values"]["/sys/class/thermal/thermal_zone2/temp"], 45.0)
        self.assertEqual(result["capabilities"]["gpu.temperature"]["values"]["/sys/class/thermal/thermal_zone8/temp"], 47.0)

    def test_required_missing_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = PlatformProbe(self.platform(Path(tmp)), MappingProbeBackend({}))
            result = probe.probe()
            self.assertFalse(result["supported"])
            with self.assertRaisesRegex(ProbeError, "gpu.frequency"):
                probe.require(result, ["gpu.frequency"])

    def test_profile_scope_preserves_platform_gaps_without_blocking_cpu(self) -> None:
        files = {
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "2100000\n",
            "/sys/devices/system/cpu/cpu0/online": "1\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            probe = PlatformProbe(self.platform(Path(tmp)), MappingProbeBackend(files))
            result = probe.probe(full=True)
            self.assertFalse(result["supported"])
            probe.apply_required_scope(
                result,
                ["cpu.frequency", "cpu.online"],
                scope="profile:cpu-test",
            )
        self.assertTrue(result["supported"])
        self.assertEqual(result["required_missing"], [])
        self.assertIn("gpu.frequency", result["platform_required_missing"])
        self.assertEqual(result["required_scope"]["name"], "profile:cpu-test")

    def test_domain_scoped_probe_does_not_scan_unrelated_target(self) -> None:
        files = {
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "2100000\n",
            "/sys/devices/system/cpu/cpu0/online": "1\n",
            "/sys/class/devfreq/gpu/cur_freq": "700000000\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = PlatformProbe(self.platform(Path(tmp)), MappingProbeBackend(files)).probe(
                full=True, domains=("cpu",)
            )
        self.assertEqual(result["probe_domains"], ["cpu"])
        self.assertIn("cpu.frequency", result["capabilities"])
        self.assertNotIn("gpu.frequency", result["capabilities"])

    def test_auto_temperature_unit_preserves_raw_and_handles_mixed_scales(self) -> None:
        files = {
            "/sys/class/thermal/thermal_zone0/type": "cpu-soc\n",
            "/sys/class/thermal/thermal_zone0/temp": "31074\n",
            "/sys/class/thermal/thermal_zone1/type": "gpu\n",
            "/sys/class/thermal/thermal_zone1/temp": "30\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = PlatformProbe(
                self.platform(Path(tmp), temperature_unit="auto"),
                MappingProbeBackend(files),
            ).probe(full=True)
        cpu = result["thermal_zones"]["zones"]["cpu"][0]
        gpu = result["thermal_zones"]["zones"]["gpu"][0]
        self.assertEqual(cpu["raw_value"], "31074")
        self.assertEqual(cpu["temperature_c"], 31.074)
        self.assertEqual(cpu["temperature_unit_applied"], "millidegree_celsius")
        self.assertEqual(gpu["raw_value"], "30")
        self.assertEqual(gpu["temperature_c"], 30.0)
        self.assertEqual(gpu["temperature_unit_applied"], "degree_celsius")

    def test_cpu_topology_falls_back_to_proc_stat(self) -> None:
        files = {
            "/proc/stat": (
                "cpu  10 0 10 100 0 0 0 0 0 0\n"
                "cpu0 5 0 5 50 0 0 0 0 0 0\n"
                "cpu1 5 0 5 50 0 0 0 0 0 0\n"
            )
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = PlatformProbe(self.platform(Path(tmp)), MappingProbeBackend(files)).probe(full=True)
        self.assertEqual(result["cpu_topology"]["core_count"], 2)
        self.assertEqual(result["cpu_topology"]["source"], "/proc/stat")

    def test_cpu_topology_maps_policies_to_related_and_affected_cores(self) -> None:
        files = {
            "/sys/devices/system/cpu/cpu0/online": "1\n",
            "/sys/devices/system/cpu/cpu1/online": "1\n",
            "/sys/devices/system/cpu/cpu2/online": "1\n",
            "/sys/devices/system/cpu/cpu3/online": "1\n",
            "/sys/devices/system/cpu/cpufreq/policy0/affected_cpus": "0 1\n",
            "/sys/devices/system/cpu/cpufreq/policy0/related_cpus": "0-1\n",
            "/sys/devices/system/cpu/cpufreq/policy2/affected_cpus": "2\n",
            "/sys/devices/system/cpu/cpufreq/policy2/related_cpus": "2,3\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = PlatformProbe(self.platform(Path(tmp)), MappingProbeBackend(files)).probe(
                full=True, domains=("cpu",)
            )
        topology = result["cpu_topology"]
        self.assertEqual(topology["policy_by_cpu"], {"0": [0], "1": [0], "2": [2], "3": [2]})
        self.assertEqual(topology["policies"][1]["affected_cpus"], [2])
        self.assertEqual(topology["policies"][1]["related_cpus"], [2, 3])
        self.assertEqual(topology["cores"][3]["policies"], [2])

    def test_required_platform_identity_fails_closed_and_survives_profile_scope(self) -> None:
        identity = {
            "required": True,
            "fields": {
                "hardware": {
                    "path": "/proc/cmdline",
                    "parser": "kernel_cmdline",
                    "key": "ohos.boot.hardware",
                    "accepted": ["Kirin9020"],
                }
            },
        }
        files = {
            "/proc/cmdline": "console=ttyHW0 ohos.boot.hardware=Kirin9030\n",
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "2100000\n",
            "/sys/devices/system/cpu/cpu0/online": "1\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            probe = PlatformProbe(self.platform(Path(tmp), identity=identity), MappingProbeBackend(files))
            result = probe.probe(full=True, domains=("cpu",))
            probe.apply_required_scope(result, ["cpu.frequency", "cpu.online"], scope="profile:cpu-test")
        self.assertFalse(result["supported"])
        self.assertEqual(result["platform_identity"]["fields"]["hardware"]["actual"], "Kirin9030")
        self.assertEqual(result["required_missing"], ["platform.identity.hardware"])

    def test_required_platform_identity_accepts_configured_value_case_insensitively(self) -> None:
        identity = {
            "required": True,
            "fields": {
                "hardware": {
                    "path": "/proc/cmdline",
                    "parser": "kernel_cmdline",
                    "key": "ohos.boot.hardware",
                    "accepted": ["Kirin9030"],
                }
            },
        }
        files = {"/proc/cmdline": "ohos.boot.hardware=kirin9030\n"}
        with tempfile.TemporaryDirectory() as tmp:
            result = PlatformProbe(
                self.platform(Path(tmp), identity=identity), MappingProbeBackend(files)
            ).probe(full=False, domains=("cpu",))
        self.assertTrue(result["platform_identity"]["matched"])
        self.assertNotIn("platform.identity.hardware", result["required_missing"])

    def test_requested_governor_must_be_supported_by_every_governor_path(self) -> None:
        governor = {
            "candidates": ["/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"],
            "available_values_candidates": [
                "/sys/devices/system/cpu/cpufreq/policy*/scaling_available_governors"
            ],
            "require_requested_value": True,
            "required": True,
        }
        files = {
            "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor": "misc\n",
            "/sys/devices/system/cpu/cpufreq/policy0/scaling_available_governors": "misc performance\n",
            "/sys/devices/system/cpu/cpufreq/policy1/scaling_governor": "misc\n",
            "/sys/devices/system/cpu/cpufreq/policy1/scaling_available_governors": "misc powersave\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            probe = PlatformProbe(
                self.platform(Path(tmp), cpu_governor=governor), MappingProbeBackend(files)
            )
            result = probe.probe(full=True, domains=("cpu",))
            probe.apply_requested_value_preflight(result, "cpu.governor", "performance")
        governor_result = result["capabilities"]["cpu.governor"]
        self.assertFalse(result["supported"])
        self.assertIn("cpu.governor.requested_value", result["required_missing"])
        self.assertEqual(
            governor_result["requested_value_preflight"]["unsupported_paths"],
            ["/sys/devices/system/cpu/cpufreq/policy1/scaling_governor"],
        )
        self.assertEqual(
            governor_result["supported_values_by_path"][
                "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
            ],
            ["misc", "performance"],
        )

    def test_requested_governor_preflight_accepts_supported_value(self) -> None:
        governor = {
            "candidates": ["/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"],
            "available_values_candidates": [
                "/sys/devices/system/cpu/cpufreq/policy*/scaling_available_governors"
            ],
            "require_requested_value": True,
            "required": True,
        }
        files = {
            "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor": "misc\n",
            "/sys/devices/system/cpu/cpufreq/policy0/scaling_available_governors": "misc performance\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            probe = PlatformProbe(
                self.platform(Path(tmp), cpu_governor=governor), MappingProbeBackend(files)
            )
            result = probe.probe(full=True, domains=("cpu",))
            probe.apply_requested_value_preflight(result, "cpu.governor", "performance")
        self.assertTrue(result["capabilities"]["cpu.governor"]["requested_value_preflight"]["verified"])
        self.assertNotIn("cpu.governor.requested_value", result["required_missing"])

    def test_requested_governor_preflight_fails_when_supported_values_are_unreadable(self) -> None:
        governor = {
            "candidates": ["/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"],
            "available_values_candidates": [
                "/sys/devices/system/cpu/cpufreq/policy*/scaling_available_governors"
            ],
            "require_requested_value": True,
            "required": True,
        }
        files = {"/sys/devices/system/cpu/cpufreq/policy0/scaling_governor": "misc\n"}
        with tempfile.TemporaryDirectory() as tmp:
            probe = PlatformProbe(
                self.platform(Path(tmp), cpu_governor=governor), MappingProbeBackend(files)
            )
            result = probe.probe(full=True, domains=("cpu",))
            probe.apply_requested_value_preflight(result, "cpu.governor", "performance")
        self.assertFalse(result["supported"])
        self.assertEqual(
            result["capabilities"]["cpu.governor"]["requested_value_preflight"]["unverified_paths"],
            ["/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"],
        )


if __name__ == "__main__":
    unittest.main()
