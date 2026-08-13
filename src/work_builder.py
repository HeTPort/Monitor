"""
Workload Command Builder - Serial Redirect Command Generation

Builds shell commands for serial port redirection.
Handles path quoting, argument escaping, and background execution.

Per DEVELOPMENT.md naming conventions:
- Use abstract names (workload, target) not specific (gpu, cpu)
- Profile-based configuration

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import logging
import shlex
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

# Configure module logger
logger = logging.getLogger(__name__)


# Characters that require shell quoting
_NEEDS_QUOTING_CHARS = {' ', "'", '"', '$', '`', '\\', '\n', '\t', '&', '|', ';', '<', '>', '(', ')'}


class SerialRedirectCommandBuilder:
    """
    Builds shell commands with serial port redirection.

    Handles:
    - Path quoting for paths with spaces or special characters
    - Argument escaping for shell safety
    - Serial redirect syntax (> device 2>&1)
    - Optional background execution (&)

    Examples:
        builder = SerialRedirectCommandBuilder()

        # Basic usage
        cmd = builder.build("/data/local/tmp/workload", "/dev/ttyAMA0")
        # Output: /data/local/tmp/workload > /dev/ttyAMA0 2>&1

        # With arguments
        cmd = builder.build(
            workload_path="/data/local/tmp/workload",
            serial_device="/dev/ttyAMA0",
            args=["--api", "vulkan", "--duration", "10"]
        )
        # Output: /data/local/tmp/workload --api vulkan --duration 10 > /dev/ttyAMA0 2>&1

        # Background execution
        cmd = builder.build("/path/workload", "/dev/ttyAMA0", background=True)
        # Output: (/path/workload > /dev/ttyAMA0 2>&1) &

        # Path with spaces (auto-quoted)
        cmd = builder.build("/data/local tmp/workload", "/dev/ttyAMA0")
        # Output: '/data/local tmp/workload' > /dev/ttyAMA0 2>&1

        # From profile
        from workload_profiles import WorkloadProfile
        profile = WorkloadProfile(name="test", target="gpu", ...)
        cmd = builder.build_from_profile(profile)
    """

    def __init__(self):
        """Initialize the command builder."""
        self._default_redirect_suffix = "2>&1"

    def build(
        self,
        workload_path: str,
        serial_device: str,
        args: Optional[List[str]] = None,
        background: bool = False
    ) -> str:
        """
        Build a serial redirect command.

        Args:
            workload_path: Path to workload binary on device.
            serial_device: Device serial port path.
            args: Optional list of workload arguments.
            background: If True, add '&' for background execution.

        Returns:
            Shell command string ready for execution.

        Raises:
            ValueError: If workload_path or serial_device is empty.
        """
        if not workload_path:
            raise ValueError("workload_path cannot be empty")
        if not serial_device:
            raise ValueError("serial_device cannot be empty")

        # Build command parts
        cmd_parts = []

        # 1. Workload path (with quoting if needed)
        cmd_parts.append(self._quote_if_needed(workload_path))

        # 2. Workload arguments
        if args:
            for arg in args:
                cmd_parts.append(self._escape_arg(arg))

        # 3. Serial redirect
        cmd_parts.append(self._build_redirect(serial_device))

        # Join parts
        command = ' '.join(cmd_parts)

        # Add background marker if requested
        if background:
            command = f"({command}) &"

        return command

    def build_from_profile(
        self,
        profile: "WorkloadProfile",
        args: Optional[List[str]] = None,
        serial_device: Optional[str] = None,
        background: bool = False
    ) -> str:
        """
        Build command from a WorkloadProfile.

        Args:
            profile: WorkloadProfile instance.
            args: Override args (uses profile defaults if None).
            serial_device: Override device (uses profile default if None).
            background: Run in background.

        Returns:
            Shell command string.
        """
        # Use profile values with potential overrides
        workload_path = profile.workload_path
        workload_args = args if args is not None else profile.default_args
        device = serial_device if serial_device else profile.serial_device

        # Log if using pending profile
        if profile.is_pending:
            logger.warning(
                f"Building command for pending profile: {profile.name}. "
                f"Workload may not exist: {workload_path}"
            )

        return self.build(
            workload_path=workload_path,
            serial_device=device,
            args=workload_args,
            background=background
        )

    def _quote_if_needed(self, path: str) -> str:
        """
        Quote path if it contains special characters.

        Args:
            path: Path string to potentially quote.

        Returns:
            Quoted path if needed, original path otherwise.
        """
        if not path:
            return path

        # Check if quoting is needed
        needs_quotes = any(c in path for c in _NEEDS_QUOTING_CHARS)

        if needs_quotes:
            # Use shlex.quote for proper shell quoting
            # This handles single quotes, escaping, etc.
            return shlex.quote(path)

        return path

    def _escape_arg(self, arg: str) -> str:
        """
        Escape a shell argument.

        Uses shlex.quote for proper handling of special characters.

        Args:
            arg: Argument string to escape.

        Returns:
            Escaped argument string.
        """
        if not arg:
            return '""'
        return shlex.quote(arg)

    def _build_redirect(self, serial_device: str) -> str:
        """
        Build the redirect portion of the command.

        Args:
            serial_device: Device serial port path.

        Returns:
            Redirect string (e.g., "> /dev/ttyAMA0 2>&1")
        """
        device = self._quote_if_needed(serial_device)
        return f"> {device} {self._default_redirect_suffix}"


@dataclass
class WorkloadCommandConfig:
    """
    Configuration for workload command building.

    Provides fine-grained control over command generation.
    """
    # Serial redirect settings
    redirect_stderr: bool = True
    redirect_stdout: bool = True

    # Execution settings
    use_subshell: bool = True  # Wrap in () for background
    use_shell: bool = True      # Use shell to execute

    # Path quoting
    auto_quote_paths: bool = True

    # Serial device defaults
    default_device: str = "/dev/ttyAMA0"


class WorkloadCommandBuilder:
    """
    High-level workload command builder using profile registry.

    Combines:
    - SerialRedirectCommandBuilder for low-level command building
    - WorkloadProfileRegistry for profile lookup

    Per DEVELOPMENT.md: Abstract naming, no GPU/CPU hardcoding.

    Usage:
        # Create with registry
        registry = WorkloadProfileRegistry("config/workload_profiles.yaml")
        builder = WorkloadCommandBuilder(registry)

        # Build from profile name
        cmd = builder.build("gpu_vulkan_game_light")

        # Build with overrides
        cmd = builder.build("gpu_vulkan_game_light", args=["--duration", "60"])

        # Build custom command
        cmd = builder.build_custom(
            workload_path="/data/local/tmp/my-workload",
            serial_device="/dev/ttyUSB0",
            args=["--mode", "test"]
        )
    """

    def __init__(
        self,
        registry: "WorkloadProfileRegistry",
        config: Optional[WorkloadCommandConfig] = None
    ):
        """
        Initialize command builder.

        Args:
            registry: WorkloadProfileRegistry instance.
            config: Optional configuration overrides.
        """
        self._registry = registry
        self._config = config or WorkloadCommandConfig()
        self._redirect_builder = SerialRedirectCommandBuilder()

    @property
    def config(self) -> WorkloadCommandConfig:
        """Get command builder configuration."""
        return self._config

    def build(
        self,
        profile_name: str,
        args: Optional[List[str]] = None,
        serial_device: Optional[str] = None,
        background: bool = False
    ) -> str:
        """
        Build command from a profile name.

        Args:
            profile_name: Name of profile in registry (e.g., "gpu_vulkan_game_light")
            args: Override profile default arguments
            serial_device: Override profile default serial device
            background: Run in background

        Returns:
            Shell command string.

        Raises:
            ValueError: If profile not found.
        """
        profile = self._registry.get(profile_name)

        if profile is None:
            available = [p.name for p in self._registry.list_all()]
            raise ValueError(
                f"Profile '{profile_name}' not found. "
                f"Available: {available}"
            )

        return self._redirect_builder.build_from_profile(
            profile=profile,
            args=args,
            serial_device=serial_device,
            background=background
        )

    def build_custom(
        self,
        workload_path: str,
        serial_device: Optional[str] = None,
        args: Optional[List[str]] = None,
        background: bool = False
    ) -> str:
        """
        Build command for a custom workload (no profile).

        Args:
            workload_path: Full path to workload binary on device.
            serial_device: Serial device for output redirection.
            args: Workload arguments.
            background: Run in background.

        Returns:
            Shell command string.
        """
        device = serial_device or self._config.default_device

        return self._redirect_builder.build(
            workload_path=workload_path,
            serial_device=device,
            args=args,
            background=background
        )

    def build_profile(self, profile: "WorkloadProfile") -> str:
        """
        Build command from a WorkloadProfile instance.

        Args:
            profile: WorkloadProfile instance.

        Returns:
            Shell command string.
        """
        return self._redirect_builder.build_from_profile(profile)

    def list_available(self) -> List[str]:
        """
        List names of available (implemented) profiles.

        Returns:
            List of profile names that can be used with build().
        """
        return [p.name for p in self._registry.list_available()]

    def get_profile(self, name: str) -> Optional["WorkloadProfile"]:
        """
        Get a profile by name.

        Args:
            name: Profile name.

        Returns:
            WorkloadProfile or None.
        """
        return self._registry.get(name)


def create_workload_command_builder(
    profile_config_path: str = None
) -> WorkloadCommandBuilder:
    """
    Factory function to create a configured WorkloadCommandBuilder.

    Args:
        profile_config_path: Path to workload_profiles.yaml.
                           Uses default location if None.

    Returns:
        Configured WorkloadCommandBuilder instance.
    """
    from workload_profiles import WorkloadProfileRegistry, get_default_config_path

    if profile_config_path is None:
        profile_config_path = get_default_config_path()

    registry = WorkloadProfileRegistry(profile_config_path)
    registry.load()

    return WorkloadCommandBuilder(registry)
