from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path, PurePosixPath

from src.path_resolver import PathResolutionError, PathResolver


class PathResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cwd = root / "cwd"
        self.config = root / "config"
        self.exe = root / "exe"
        self.bundle = root / "bundle"
        self.state = root / "state"
        self.output = root / "output"
        for directory in (self.cwd, self.config, self.exe, self.bundle):
            directory.mkdir(parents=True)
        self.resolver = PathResolver(
            bundle_root=self.bundle,
            exe_root=self.exe,
            state_root=self.state,
            output_root=self.output,
            cwd=self.cwd,
            config_dir=self.config,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_input_precedence(self) -> None:
        for directory, value in (
            (self.bundle, "bundle"),
            (self.exe, "exe"),
            (self.config, "config"),
            (self.cwd, "cwd"),
        ):
            (directory / "same.yaml").write_text(value, encoding="utf-8")
        self.assertEqual(self.resolver.resolve_input("same.yaml").read_text(encoding="utf-8"), "config")

    def test_config_override_accepts_mirrored_or_direct_config_root(self) -> None:
        direct = self.config / "platforms" / "kirin9020.yaml"
        direct.parent.mkdir()
        direct.write_text("direct", encoding="utf-8")
        bundled = self.bundle / "config" / "platforms" / "kirin9020.yaml"
        bundled.parent.mkdir(parents=True)
        bundled.write_text("bundle", encoding="utf-8")
        resolved = self.resolver.resolve_input("config/platforms/kirin9020.yaml")
        self.assertEqual(resolved, direct.resolve())

    def test_owner_relative_reference_precedes_global_roots(self) -> None:
        owner_dir = self.config / "profiles"
        owner_dir.mkdir()
        owner = owner_dir / "cpu.yaml"
        owner.write_text("profile", encoding="utf-8")
        referenced = owner_dir / "workload.json"
        referenced.write_text("{}", encoding="utf-8")
        (self.cwd / "workload.json").write_text("wrong", encoding="utf-8")
        self.assertEqual(self.resolver.resolve_input("workload.json", owner=owner), referenced.resolve())

    def test_output_state_and_remote_roots(self) -> None:
        output = self.resolver.resolve_output("run/result.json", create_parent=True)
        state = self.resolver.resolve_state("registry/index.json", create_parent=True)
        self.assertEqual(output.parent, (self.output / "run").resolve())
        self.assertEqual(state.parent, (self.state / "registry").resolve())
        self.assertEqual(
            self.resolver.remote(r"runs\abc\manifest.json"),
            PurePosixPath("/data/local/tmp/avs/runs/abc/manifest.json"),
        )

    def test_missing_required_input_reports_search(self) -> None:
        with self.assertRaisesRegex(PathResolutionError, "searched"):
            self.resolver.resolve_input("missing.file")

    def test_frozen_bundle_and_executable_roots_are_distinct(self) -> None:
        frozen_executable = self.exe / "vmin_judge.exe"
        with patch.object(__import__("sys"), "frozen", True, create=True), patch.object(
            __import__("sys"), "_MEIPASS", str(self.bundle), create=True
        ), patch.object(__import__("sys"), "executable", str(frozen_executable)):
            resolver = PathResolver.create(
                cwd=self.cwd,
                state_dir=self.state,
                output_dir=self.output,
            )
        self.assertEqual(resolver.bundle_root, self.bundle.resolve())
        self.assertEqual(resolver.exe_root, self.exe.resolve())
        self.assertEqual(resolver.cwd, self.cwd.resolve())


if __name__ == "__main__":
    unittest.main()
