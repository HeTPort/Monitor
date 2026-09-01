"""Typed ADB/HDC transport commands and deterministic fake transport."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


class TransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceIdentity:
    transport: str
    serial: str
    state: str = "device"
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def transport_ok(self) -> bool:
        return not self.timed_out and self.return_code >= 0

    @property
    def success(self) -> bool:
        return self.transport_ok and self.return_code == 0


@dataclass(frozen=True)
class TransferResult:
    local: Path
    remote: PurePosixPath
    direction: str
    success: bool
    bytes_transferred: int | None
    duration_s: float
    message: str = ""


class Transport(ABC):
    name: str

    @abstractmethod
    def connect(self) -> DeviceIdentity:
        raise NotImplementedError

    @abstractmethod
    def invoke(self, argv: Sequence[str], timeout_s: float = 30.0) -> CommandResult:
        raise NotImplementedError

    @abstractmethod
    def push(self, local: Path, remote: PurePosixPath, timeout_s: float = 60.0) -> TransferResult:
        raise NotImplementedError

    @abstractmethod
    def pull(self, remote: PurePosixPath, local: Path, timeout_s: float = 60.0) -> TransferResult:
        raise NotImplementedError

    def cancel_active(self) -> int:
        """Request cancellation of active host-side transport processes."""
        return 0

    def sha256(self, remote: PurePosixPath) -> str:
        for command in (("sha256sum", str(remote)), ("toybox", "sha256sum", str(remote))):
            result = self.invoke(command, timeout_s=30.0)
            if result.success and result.stdout.strip():
                value = result.stdout.strip().split()[0].lower()
                if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
                    return value
        raise TransportError(f"unable to calculate remote SHA-256: {remote}")

    def mkdir(self, remote: PurePosixPath) -> None:
        result = self.invoke(("mkdir", "-p", str(remote)))
        if not result.success:
            raise TransportError(f"remote mkdir failed for {remote}: {result.stderr or result.stdout}")

    def chmod(self, remote: PurePosixPath, mode: str) -> None:
        result = self.invoke(("chmod", mode, str(remote)))
        if not result.success:
            raise TransportError(f"remote chmod failed for {remote}: {result.stderr or result.stdout}")


class SubprocessTransport(Transport):
    """Common host subprocess mechanics; subclasses define ADB/HDC argv."""

    def __init__(self, tool: Path, *, serial: str | None = None):
        self.tool = tool.expanduser().resolve(strict=False)
        self.serial = serial
        self._process_lock = threading.Lock()
        self._active_processes: set[subprocess.Popen[str]] = set()

    @abstractmethod
    def _host_prefix(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def _shell_command(self, argv: Sequence[str]) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def _push_command(self, local: Path, remote: PurePosixPath) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def _pull_command(self, remote: PurePosixPath, local: Path) -> list[str]:
        raise NotImplementedError

    def invoke(self, argv: Sequence[str], timeout_s: float = 30.0) -> CommandResult:
        remote_argv = self._validate_argv(argv)
        return self._run(self._shell_command(remote_argv), timeout_s)

    def push(self, local: Path, remote: PurePosixPath, timeout_s: float = 60.0) -> TransferResult:
        source = local.expanduser().resolve(strict=True)
        started = time.monotonic()
        result = self._run(self._push_command(source, remote), timeout_s)
        return TransferResult(
            local=source,
            remote=remote,
            direction="push",
            success=result.success,
            bytes_transferred=source.stat().st_size if result.success else None,
            duration_s=time.monotonic() - started,
            message=result.stderr or result.stdout,
        )

    def pull(self, remote: PurePosixPath, local: Path, timeout_s: float = 60.0) -> TransferResult:
        destination = local.expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        result = self._run(self._pull_command(remote, destination), timeout_s)
        return TransferResult(
            local=destination,
            remote=remote,
            direction="pull",
            success=result.success,
            bytes_transferred=destination.stat().st_size if result.success and destination.exists() else None,
            duration_s=time.monotonic() - started,
            message=result.stderr or result.stdout,
        )

    def _run(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        command = [str(item) for item in argv]
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            with self._process_lock:
                self._active_processes.add(process)
            stdout, stderr = process.communicate(timeout=timeout_s)
            return CommandResult(
                argv=tuple(command),
                return_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_s=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                process.kill()
                stdout, stderr = process.communicate()
            else:
                stdout, stderr = "", ""
            return CommandResult(
                argv=tuple(command),
                return_code=-1,
                stdout=stdout or ((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
                stderr=stderr or ((exc.stderr or "") if isinstance(exc.stderr, str) else f"timeout after {timeout_s}s"),
                duration_s=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as exc:
            raise TransportError(f"failed to execute {command[0]}: {exc}") from exc
        finally:
            if process is not None:
                with self._process_lock:
                    self._active_processes.discard(process)

    def cancel_active(self) -> int:
        """Terminate active ADB/HDC host processes so a timed-out run can return."""
        with self._process_lock:
            processes = tuple(self._active_processes)
        cancelled = 0
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                process.terminate()
                cancelled += 1
            except OSError:
                continue
        return cancelled

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> list[str]:
        if not argv or not all(isinstance(item, str) and "\x00" not in item for item in argv):
            raise TransportError("remote argv must be a non-empty sequence of strings without NUL")
        return list(argv)


class ADBTransport(SubprocessTransport):
    name = "adb"

    def _host_prefix(self) -> list[str]:
        prefix = [str(self.tool)]
        if self.serial:
            prefix.extend(("-s", self.serial))
        return prefix

    def _shell_command(self, argv: Sequence[str]) -> list[str]:
        return [*self._host_prefix(), "shell", *argv]

    def _push_command(self, local: Path, remote: PurePosixPath) -> list[str]:
        return [*self._host_prefix(), "push", str(local), str(remote)]

    def _pull_command(self, remote: PurePosixPath, local: Path) -> list[str]:
        return [*self._host_prefix(), "pull", str(remote), str(local)]

    def connect(self) -> DeviceIdentity:
        result = self._run([*self._host_prefix(), "get-state"], 10.0)
        if not result.success or result.stdout.strip() != "device":
            raise TransportError(f"ADB device is unavailable: {result.stderr or result.stdout}")
        serial = self.serial
        if not serial:
            serial_result = self._run([*self._host_prefix(), "get-serialno"], 10.0)
            if not serial_result.success or not serial_result.stdout.strip():
                raise TransportError("ADB could not determine device serial")
            serial = serial_result.stdout.strip()
        return DeviceIdentity(transport=self.name, serial=serial, state="device")


class HDCTransport(SubprocessTransport):
    name = "hdc"
    _REMOTE_RC_MARKER = "__VMIN_REMOTE_RC__="

    def _host_prefix(self) -> list[str]:
        prefix = [str(self.tool)]
        if self.serial:
            prefix.extend(("-t", self.serial))
        return prefix

    def _shell_command(self, argv: Sequence[str]) -> list[str]:
        return [*self._host_prefix(), "shell", *argv]

    def invoke(self, argv: Sequence[str], timeout_s: float = 30.0) -> CommandResult:
        """Execute through the device shell and return the remote command status.

        HDC can return host status zero even when `/bin/sh` reports a missing
        command or path.  Add a shell-owned status marker and remove it from
        captured output so probes and deployment do not treat shell errors as
        successful device operations.
        """
        remote_argv = self._validate_argv(argv)
        remote_command = shlex.join(remote_argv)
        script = (
            f"{remote_command}; __vmin_rc=$?; "
            f"echo {self._REMOTE_RC_MARKER}$__vmin_rc"
        )
        host_result = self._run([*self._host_prefix(), "shell", script], timeout_s)
        if host_result.timed_out or host_result.return_code != 0:
            return host_result
        marker_index = host_result.stdout.rfind(self._REMOTE_RC_MARKER)
        if marker_index < 0:
            detail = "HDC shell did not return a remote exit status"
            stderr = f"{host_result.stderr.rstrip()}\n{detail}".strip()
            return CommandResult(tuple(remote_argv), -1, host_result.stdout, stderr, host_result.duration_s)
        status_text = host_result.stdout[marker_index + len(self._REMOTE_RC_MARKER):].strip().splitlines()[0]
        try:
            remote_status = int(status_text)
        except ValueError:
            detail = f"invalid HDC remote exit status: {status_text!r}"
            stderr = f"{host_result.stderr.rstrip()}\n{detail}".strip()
            return CommandResult(tuple(remote_argv), -1, host_result.stdout[:marker_index], stderr, host_result.duration_s)
        return CommandResult(
            tuple(remote_argv),
            remote_status,
            host_result.stdout[:marker_index],
            host_result.stderr,
            host_result.duration_s,
        )

    def _push_command(self, local: Path, remote: PurePosixPath) -> list[str]:
        return [*self._host_prefix(), "file", "send", str(local), str(remote)]

    def _pull_command(self, remote: PurePosixPath, local: Path) -> list[str]:
        return [*self._host_prefix(), "file", "recv", str(remote), str(local)]

    def connect(self) -> DeviceIdentity:
        result = self._run([*self._host_prefix(), "list", "targets"], 10.0)
        targets = [line.strip().split()[0] for line in result.stdout.splitlines() if line.strip()]
        if not result.success or not targets:
            raise TransportError(f"HDC device is unavailable: {result.stderr or result.stdout}")
        if self.serial and self.serial not in targets:
            raise TransportError(f"HDC target not found: {self.serial}")
        if not self.serial and len(targets) != 1:
            raise TransportError(f"HDC target is ambiguous: {targets}")
        return DeviceIdentity(transport=self.name, serial=self.serial or targets[0], state="device")


class TransportManager:
    """Connect transports in requested order; auto prefers HDC for HarmonyOS."""

    def __init__(self, transports: Iterable[Transport]):
        self.transports = list(transports)
        self.active: Transport | None = None

    def connect(self) -> DeviceIdentity:
        errors: list[str] = []
        for transport in self.transports:
            try:
                identity = transport.connect()
                self.active = transport
                return identity
            except TransportError as exc:
                errors.append(f"{transport.name}: {exc}")
        raise TransportError("no transport connected; " + "; ".join(errors))

    def require_active(self) -> Transport:
        if self.active is None:
            raise TransportError("transport manager is not connected")
        return self.active


class FakeTransport(Transport):
    """In-memory remote filesystem and command recorder for integration tests."""

    name = "fake"

    def __init__(self, files: dict[str, bytes] | None = None, serial: str = "FAKE-001"):
        self.files = dict(files or {})
        self.serial = serial
        self.commands: list[tuple[str, ...]] = []
        self.push_count = 0

    def connect(self) -> DeviceIdentity:
        return DeviceIdentity(transport=self.name, serial=self.serial)

    def invoke(self, argv: Sequence[str], timeout_s: float = 30.0) -> CommandResult:
        command = tuple(argv)
        self.commands.append(command)
        stdout = ""
        return_code = 0
        if command[:2] == ("mkdir", "-p") or command[:1] == ("chmod",):
            pass
        elif command and command[0] in {"sha256sum", "toybox"}:
            remote = command[-1]
            if remote not in self.files:
                return_code = 1
            else:
                stdout = f"{hashlib.sha256(self.files[remote]).hexdigest()}  {remote}\n"
        elif command[:2] == ("test", "-e"):
            return_code = 0 if command[-1] in self.files else 1
        elif command[:3] == ("rm", "-f", "--"):
            self.files.pop(command[-1], None)
        else:
            stdout = ""
        return CommandResult(command, return_code, stdout, "", 0.0)

    def push(self, local: Path, remote: PurePosixPath, timeout_s: float = 60.0) -> TransferResult:
        source = local.expanduser().resolve(strict=True)
        self.files[str(remote)] = source.read_bytes()
        self.push_count += 1
        return TransferResult(source, remote, "push", True, source.stat().st_size, 0.0)

    def pull(self, remote: PurePosixPath, local: Path, timeout_s: float = 60.0) -> TransferResult:
        destination = local.expanduser().resolve(strict=False)
        data = self.files.get(str(remote))
        if data is None:
            return TransferResult(destination, remote, "pull", False, None, 0.0, "not found")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return TransferResult(destination, remote, "pull", True, len(data), 0.0)
