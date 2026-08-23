from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PurePosixPath

from src.deployment import AssetSpec, DeploymentError, DeploymentManager
from src.transport import FakeTransport, TransportError, TransportManager


class FailingTransport(FakeTransport):
    name = "failing"

    def connect(self):
        raise TransportError("offline")


class TransportDeploymentTests(unittest.TestCase):
    def test_manager_falls_back_and_deployment_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "agent"
            local.write_bytes(b"agent-v1")
            fake = FakeTransport()
            manager = TransportManager([FailingTransport(), fake])
            identity = manager.connect()
            self.assertEqual(identity.transport, "fake")
            deployer = DeploymentManager(manager.require_active())
            asset = AssetSpec(local, PurePosixPath("/data/local/tmp/avs/bin/agent"), executable=True, kind="agent")
            first = deployer.deploy([asset], verify_hashes=True)
            second = deployer.deploy([asset], verify_hashes=True)
            self.assertEqual(first["assets"][0]["action"], "pushed")
            self.assertEqual(second["assets"][0]["action"], "unchanged")
            self.assertEqual(fake.push_count, 1)
            self.assertIn(("chmod", "0755", "/data/local/tmp/avs/bin/agent"), fake.commands)

    def test_required_missing_asset_fails_and_optional_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            deployer = DeploymentManager(FakeTransport())
            with self.assertRaisesRegex(DeploymentError, "required"):
                deployer.deploy([AssetSpec(missing, PurePosixPath("/remote/missing"))])
            manifest = deployer.deploy(
                [AssetSpec(missing, PurePosixPath("/remote/optional"), required=False)], verify_hashes=True
            )
            self.assertEqual(manifest["assets"][0]["action"], "missing-optional")

    def test_clean_stale_removes_only_previous_manifest_assets_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "current"
            local.write_bytes(b"current")
            stale = "/data/local/tmp/avs/bin/stale"
            fake = FakeTransport({stale: b"old"})
            previous = {"assets": [{"remote": stale}]}
            manifest = DeploymentManager(fake).deploy(
                [AssetSpec(local, PurePosixPath("/data/local/tmp/avs/bin/current"))],
                clean_stale=True,
                previous_manifest=previous,
                allowed_remote_root=PurePosixPath("/data/local/tmp/avs"),
            )
            self.assertEqual(manifest["removed_stale"], [stale])
            self.assertNotIn(stale, fake.files)
            with self.assertRaisesRegex(DeploymentError, "outside managed root"):
                DeploymentManager(fake).deploy(
                    [AssetSpec(local, PurePosixPath("/data/local/tmp/avs/bin/current"))],
                    clean_stale=True,
                    previous_manifest={"assets": [{"remote": "/data/local/tmp/not-owned"}]},
                    allowed_remote_root=PurePosixPath("/data/local/tmp/avs"),
                )


if __name__ == "__main__":
    unittest.main()
