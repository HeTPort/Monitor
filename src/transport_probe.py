"""PlatformProbe backend implemented through the typed device transport."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Mapping

from .platform_probe import ProbeBackend
from .transport import DeviceIdentity, Transport, TransportError


class TransportProbeBackend(ProbeBackend):
    def __init__(self, transport: Transport, identity: DeviceIdentity):
        self.transport = transport
        self.device_identity = identity

    def glob(self, pattern: str) -> list[str]:
        root = self._static_root(pattern)
        result = self.transport.invoke(("find", root, "-path", pattern, "-print"), timeout_s=30.0)
        if not result.success:
            return []
        return sorted({line.strip() for line in result.stdout.splitlines() if line.strip().startswith("/")})

    def exists(self, path: str) -> bool:
        return self.transport.invoke(("test", "-e", path), timeout_s=5.0).success

    def read_text(self, path: str) -> str:
        result = self.transport.invoke(("cat", path), timeout_s=5.0)
        if not result.success:
            raise OSError(result.stderr or result.stdout or f"unable to read {path}")
        return result.stdout

    def identity(self) -> Mapping[str, Any]:
        return {
            "transport": self.device_identity.transport,
            "serial": self.device_identity.serial,
            "state": self.device_identity.state,
            "properties": dict(self.device_identity.properties),
        }

    @staticmethod
    def _static_root(pattern: str) -> str:
        path = PurePosixPath(pattern)
        parts: list[str] = []
        for part in path.parts:
            if any(character in part for character in "*?["):
                break
            parts.append(part)
        if not parts:
            return "/"
        root = str(PurePosixPath(*parts))
        return root if root.startswith("/") else f"/{root}"
