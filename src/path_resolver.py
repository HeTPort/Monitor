"""Deterministic local, packaged-resource, state, output, tool, and device paths."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


class PathResolutionError(ValueError):
    """Raised when a requested path cannot be resolved safely."""


def _absolute(path: Path) -> Path:
    """Return a normalized absolute path without requiring it to exist."""
    return path.expanduser().resolve(strict=False)


@dataclass(frozen=True)
class PathResolver:
    """Resolve every path class without changing the process working directory.

    ``bundle_root`` is read-only application data, ``exe_root`` contains external
    overrides beside the executable, ``state_root`` is persistent writable state,
    and ``output_root`` owns generated qualification/run artifacts.
    """

    bundle_root: Path
    exe_root: Path
    state_root: Path
    output_root: Path
    cwd: Path
    config_dir: Path | None = None
    device_root: PurePosixPath = PurePosixPath("/data/local/tmp/avs")

    def __post_init__(self) -> None:
        for name in ("bundle_root", "exe_root", "state_root", "output_root", "cwd"):
            object.__setattr__(self, name, _absolute(getattr(self, name)))
        if self.config_dir is not None:
            object.__setattr__(self, "config_dir", _absolute(self.config_dir))
        object.__setattr__(self, "device_root", PurePosixPath(str(self.device_root)))

    @classmethod
    def create(
        cls,
        *,
        config_dir: str | os.PathLike[str] | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        output_dir: str | os.PathLike[str] | None = None,
        device_root: str = "/data/local/tmp/avs",
        cwd: str | os.PathLike[str] | None = None,
        entrypoint: str | os.PathLike[str] | None = None,
    ) -> "PathResolver":
        caller_cwd = _absolute(Path(cwd) if cwd is not None else Path.cwd())
        if getattr(sys, "frozen", False):
            executable = _absolute(Path(sys.executable))
            exe_root = executable.parent
            bundle_root = _absolute(Path(getattr(sys, "_MEIPASS", exe_root)))
        else:
            source = _absolute(Path(entrypoint) if entrypoint is not None else Path(__file__))
            exe_root = source.parent if source.is_file() or source.suffix else source
            if exe_root.name == "src":
                exe_root = exe_root.parent
            bundle_root = exe_root

        if state_dir is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            state_root = Path(local_app_data) / "VminJudge" if local_app_data else Path.home() / ".vmin_judge"
        else:
            state_root = Path(state_dir)

        output_root = Path(output_dir) if output_dir is not None else caller_cwd / "output"
        return cls(
            bundle_root=bundle_root,
            exe_root=exe_root,
            state_root=state_root,
            output_root=output_root,
            cwd=caller_cwd,
            config_dir=Path(config_dir) if config_dir is not None else None,
            device_root=PurePosixPath(device_root),
        )

    def ensure_writable_roots(self) -> None:
        """Create persistent state and output roots, never bundle/executable roots."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)

    def input_candidates(self, value: str | os.PathLike[str], *, owner: Path | None = None) -> list[Path]:
        raw = Path(value).expanduser()
        if raw.is_absolute():
            return [_absolute(raw)]

        candidates: list[Path] = []
        if owner is not None:
            owner_path = _absolute(Path(owner))
            candidates.append(owner_path.parent / raw if owner_path.suffix else owner_path / raw)
        if self.config_dir is not None:
            candidates.append(self.config_dir / raw)
            if raw.parts and raw.parts[0].lower() == "config" and len(raw.parts) > 1:
                candidates.append(self.config_dir.joinpath(*raw.parts[1:]))
        candidates.append(self.cwd / raw)
        candidates.extend((self.exe_root / raw, self.bundle_root / raw))
        return self._deduplicate(candidates)

    def resolve_input(
        self,
        value: str | os.PathLike[str],
        *,
        owner: Path | None = None,
        required: bool = True,
    ) -> Path:
        """Resolve an input according to the documented precedence contract."""
        candidates = self.input_candidates(value, owner=owner)
        for candidate in candidates:
            if candidate.exists():
                return _absolute(candidate)
        if required:
            searched = ", ".join(str(_absolute(item)) for item in candidates)
            raise PathResolutionError(f"input not found: {value}; searched: {searched}")
        return _absolute(candidates[0])

    def resolve_resource(self, relative: str | os.PathLike[str], *, required: bool = True) -> Path:
        """Resolve an external executable-side override before bundled data."""
        raw = Path(relative)
        if raw.is_absolute():
            candidates = [raw]
        else:
            candidates = [self.exe_root / raw, self.bundle_root / raw]
        for candidate in self._deduplicate(candidates):
            if candidate.exists():
                return _absolute(candidate)
        if required:
            raise PathResolutionError(f"resource not found: {relative}")
        return _absolute(candidates[0])

    def resolve_output(self, relative: str | os.PathLike[str], *, create_parent: bool = False) -> Path:
        """Resolve generated output beneath ``output_root`` unless absolute."""
        value = Path(relative).expanduser()
        result = _absolute(value if value.is_absolute() else self.output_root / value)
        if create_parent:
            result.parent.mkdir(parents=True, exist_ok=True)
        return result

    def resolve_state(self, relative: str | os.PathLike[str], *, create_parent: bool = False) -> Path:
        """Resolve persistent application data beneath ``state_root``."""
        value = Path(relative).expanduser()
        result = _absolute(value if value.is_absolute() else self.state_root / value)
        if create_parent:
            result.parent.mkdir(parents=True, exist_ok=True)
        return result

    def resolve_tool(self, name: str, explicit: str | os.PathLike[str] | None = None) -> Path:
        """Resolve ADB/HDC or another host tool by explicit, packaged, then PATH order."""
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        candidates.extend((self.exe_root / "tools" / name, self.bundle_root / "tools" / name))
        if os.name == "nt" and not name.lower().endswith(".exe"):
            candidates.extend((self.exe_root / "tools" / f"{name}.exe", self.bundle_root / "tools" / f"{name}.exe"))
        for candidate in self._deduplicate(candidates):
            if candidate.exists() and candidate.is_file():
                return _absolute(candidate)
        discovered = shutil.which(name)
        if discovered:
            return _absolute(Path(discovered))
        raise PathResolutionError(f"host tool not found: {explicit or name}")

    def remote(self, relative: str | PurePosixPath) -> PurePosixPath:
        """Return a normalized POSIX device path, independent of the host OS."""
        value = PurePosixPath(str(relative).replace("\\", "/"))
        return value if value.is_absolute() else self.device_root / value

    @staticmethod
    def _deduplicate(paths: Iterable[Path]) -> list[Path]:
        seen: set[str] = set()
        result: list[Path] = []
        for path in paths:
            normalized = _absolute(path)
            key = os.path.normcase(str(normalized))
            if key not in seen:
                seen.add(key)
                result.append(normalized)
        return result
