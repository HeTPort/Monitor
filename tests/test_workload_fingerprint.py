from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.cli_commands import _require_current_correctness, _workload_fingerprint_fields
from src.config_loader import ConfigError, ProfileConfig
from src.path_resolver import PathResolver
from src.qualification import correctness_fingerprint


class WorkloadFingerprintTests(unittest.TestCase):
    def test_shader_byte_change_invalidates_golden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_path = root / "config" / "profiles" / "gpu.json"
            config_path = root / "config" / "workloads" / "gpu.json"
            binary_path = root / "tools" / "gpu-avs-workload"
            shader_path = root / "tools" / "shaders" / "workload.frag.spv"
            agent_path = root / "device" / "avs_device_agent.py"
            for path in (profile_path, config_path, binary_path, shader_path, agent_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("{}", encoding="utf-8")
            binary_path.write_bytes(b"workload-v1")
            shader_path.write_bytes(b"shader-v1")
            agent_path.write_text("# agent", encoding="utf-8")
            profile = ProfileConfig.from_mapping(
                {
                    "schema_version": 1,
                    "name": "gpu-test",
                    "target": "gpu",
                    "platform": "fake",
                    "workload": {
                        "binary": "../../tools/gpu-avs-workload",
                        "config": "../workloads/gpu.json",
                        "assets": [
                            {
                                "local": "../../tools/shaders/workload.frag.spv",
                                "remote": "shaders/workload.frag.spv",
                            }
                        ],
                    },
                    "environment": {},
                    "telemetry": {"required": []},
                    "kernel_monitor": "off",
                },
                source_path=profile_path,
            )
            paths = PathResolver(
                bundle_root=root,
                exe_root=root,
                state_root=root / "state",
                output_root=root / "output",
                cwd=root,
            )
            fingerprint = correctness_fingerprint(_workload_fingerprint_fields(paths, profile))
            _require_current_correctness(paths, profile, {"correctness_fingerprint": fingerprint})
            shader_path.write_bytes(b"shader-v2")
            with self.assertRaisesRegex(ConfigError, "does not match golden"):
                _require_current_correctness(paths, profile, {"correctness_fingerprint": fingerprint})


if __name__ == "__main__":
    unittest.main()
