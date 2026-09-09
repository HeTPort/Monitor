from __future__ import annotations

import unittest
from pathlib import Path, PurePosixPath

from src.config_loader import PlatformConfig, ProfileConfig, validate_workload_config


ROOT = Path(__file__).resolve().parents[1]
CPU_ADDITIONS = {
    "cpu_integer_qualification_kirin9030": "integer",
    "cpu_floating_point_qualification_kirin9030": "floating_point",
    "cpu_matrix_qualification_kirin9030": "matrix",
    "cpu_memory_qualification_kirin9030": "memory",
    "cpu_burst_qualification_kirin9030": "mixed",
    "cpu_thermal_qualification_kirin9030": "mixed",
    "cpu_stress_extreme_qualification_kirin9030": "mixed",
    "cpu_idle_control_kirin9030": "null",
}
GPU_ADDITIONS = {
    "gpu_alu_qualification_kirin9030": "alu",
    "gpu_sfu_qualification_kirin9030": "sfu",
    "gpu_texture_qualification_kirin9030": "texture",
    "gpu_fill_qualification_kirin9030": "fill",
    "gpu_load_light_kirin9030": "mixed",
    "gpu_load_mid_kirin9030": "mixed",
    "gpu_load_heavy_kirin9030": "mixed",
    "gpu_thermal_kirin9030": "mixed",
    "gpu_stress_extreme_kirin9030": "mixed",
}


class ProfileCatalogTests(unittest.TestCase):
    def test_every_profile_resolves_platform_workload_and_documented_entry(self) -> None:
        catalog = (ROOT / "docs" / "PROFILE_CATALOG.md").read_text(encoding="utf-8")
        golden_paths: set[str] = set()
        for path in sorted((ROOT / "config" / "profiles").glob("*.yaml")):
            with self.subTest(profile=path.stem):
                profile = ProfileConfig.from_file(path)
                self.assertEqual(profile.name, path.stem)
                self.assertIn(f"`{profile.name}`", catalog)
                platform = PlatformConfig.from_file(ROOT / "config" / "platforms" / f"{profile.platform}.yaml")
                self.assertEqual(platform.name, profile.platform)
                config_path = (path.parent / profile.workload["config"]).resolve(strict=True)
                self.assertTrue(config_path.is_relative_to(ROOT / "config" / "workloads"))
                config = validate_workload_config(config_path, profile.target)
                self.assertEqual(config["verify_interval"], 1)
                self.assertEqual(config["success_log_interval"], 60)
                self.assertFalse(config["summary_only"])
                self.assertGreater(config["timeout"], config["duration"] + config["warmup"])
                if config["verify_mode"] == "golden-image":
                    golden_path = config["golden_file"]
                    self.assertNotIn(golden_path, golden_paths, "each GPU correctness config needs its own golden")
                    golden_paths.add(golden_path)
                if profile.target == "gpu":
                    remotes = {asset["remote"] for asset in profile.workload["assets"]}
                    self.assertEqual(remotes, {
                        "shaders/vulkan/fullscreen.vert.spv", "shaders/vulkan/workload.frag.spv"
                    })
                    for asset in profile.workload["assets"]:
                        self.assertFalse(PurePosixPath(asset["remote"]).is_absolute())
                        self.assertTrue((path.parent / asset["local"]).resolve().is_relative_to(ROOT / "tools"))

    def test_cpu_backend_coverage_and_idle_control_are_explicit(self) -> None:
        for name, backend in CPU_ADDITIONS.items():
            with self.subTest(profile=name):
                profile = ProfileConfig.from_file(ROOT / "config" / "profiles" / f"{name}.yaml")
                config = validate_workload_config(profile.source_path.parent / profile.workload["config"], "cpu")
                self.assertEqual(config["backend"], backend)
                self.assertEqual(profile.environment, {}, "new profiles must not assume board core masks/OPPs")
                self.assertFalse(config["per_batch_log"])
                if backend == "null":
                    self.assertEqual(config["verify_mode"], "none")
                    self.assertEqual(config["duty_cycle"], 0)
                else:
                    self.assertEqual(config["verify_mode"], "checksum")
                    self.assertEqual(config["threads"], 4)
                if name == "cpu_burst_qualification_kirin9030":
                    self.assertEqual(config["burst_active"] / config["burst_period"], config["duty_cycle"])

    def test_gpu_catalog_uses_real_offscreen_paths_and_distinguishes_correctness(self) -> None:
        for name, shader in GPU_ADDITIONS.items():
            with self.subTest(profile=name):
                profile = ProfileConfig.from_file(ROOT / "config" / "profiles" / f"{name}.yaml")
                config = validate_workload_config(profile.source_path.parent / profile.workload["config"], "gpu")
                self.assertEqual(profile.environment, {})
                self.assertEqual(config["api"], "vulkan")
                self.assertEqual(config["mode"], "offscreen")
                self.assertEqual(config["shader"], shader)
                self.assertEqual(config["duty_cycle"], 1)
                self.assertEqual(config["samples"], 1)
                self.assertFalse(config["per_frame_log"])
                self.assertEqual(config["verify_mode"], "golden-image" if "_qualification_" in name else "none")


if __name__ == "__main__":
    unittest.main()
