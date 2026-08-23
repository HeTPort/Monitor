"""
Workload Profiles - Profile Loader and Registry

Provides profile-based workload configuration for GPU/CPU Vmin testing.
Profiles are loaded from YAML configuration files.

Per DEVELOPMENT.md naming conventions:
- Use 'target', 'workload' not 'gpu'/'cpu' in class names
- Abstract naming for target-agnostic design

Part of the 5-layer architecture defined in ARCHITECTURE.md.

Author: Vmin Judge Tool Development
Version: 1.0
"""

from __future__ import annotations

import os
import logging
try:
    import yaml
except ImportError:  # Hardware-free help and JSON tooling remain usable.
    yaml = None
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

# Configure module logger
logger = logging.getLogger(__name__)


@dataclass
class WorkloadProfile:
    """
    Single workload profile configuration.

    Represents a configured workload that can be executed via the test
    orchestrator. Profiles can be GPU or CPU workloads.

    Attributes:
        name: Full profile name (e.g., "gpu_vulkan_game_light", "cpu_stress_baseline")
        target: Target type ("gpu" or "cpu")
        display_name: Human-readable name for UI display
        description: Detailed description of what this profile tests
        workload_path: Path to workload binary on device
        default_args: Default command-line arguments for the workload
        serial_device: Serial device path for output redirection
        expected_duration_sec: Expected test duration in seconds
        status: Implementation status ("implemented", "pending", "deprecated")
        notes: Additional notes (e.g., pending implementation notes)

    Properties:
        is_implemented: True if workload is implemented
        is_pending: True if workload is pending implementation
        is_deprecated: True if workload is deprecated
    """
    name: str
    target: str
    display_name: str
    description: str
    workload_path: str
    default_args: List[str] = field(default_factory=list)
    serial_device: str = "/dev/ttyAMA0"
    expected_duration_sec: int = 30
    status: str = "implemented"
    notes: Optional[str] = None

    # Common settings (may be applied from config)
    baudrate: int = 115200
    heartbeat_timeout_sec: int = 45
    grace_period_sec: int = 2

    @property
    def is_implemented(self) -> bool:
        """Check if workload is implemented and ready to use."""
        return self.status == "implemented"

    @property
    def is_pending(self) -> bool:
        """Check if workload is pending implementation."""
        return self.status == "pending"

    @property
    def is_deprecated(self) -> bool:
        """Check if workload is deprecated."""
        return self.status == "deprecated"

    @property
    def is_available(self) -> bool:
        """Check if workload is available for use (implemented and not deprecated)."""
        return self.is_implemented and not self.is_deprecated

    def __str__(self) -> str:
        status_icon = "✓" if self.is_implemented else "⏳"
        return f"{status_icon} {self.display_name} ({self.target.upper()})"

    def __repr__(self) -> str:
        return (
            f"WorkloadProfile(name={self.name!r}, target={self.target!r}, "
            f"status={self.status!r}, path={self.workload_path!r})"
        )


class WorkloadProfileLoader:
    """
    Loads and parses workload profiles from YAML configuration files.

    Supports the dual-section format (gpu/cpu) with common settings.

    Usage:
        loader = WorkloadProfileLoader("config/workload_profiles.yaml")
        profiles = loader.load()

        # Get specific profile
        profile = loader.get("gpu_vulkan_game_light")

        # List by target
        gpu_profiles = loader.list_gpu()
        cpu_profiles = loader.list_cpu()

        # List only implemented
        available = loader.list_implemented()
    """

    def __init__(self, config_path: str):
        """
        Initialize profile loader.

        Args:
            config_path: Path to workload_profiles.yaml file.
        """
        self._config_path = config_path
        self._profiles: Dict[str, WorkloadProfile] = {}
        self._common_settings: Dict[str, Any] = {}
        self._loaded = False
        self._version: str = "1.0"
        self._description: str = ""

    @property
    def config_path(self) -> str:
        """Get the configuration file path."""
        return self._config_path

    @property
    def version(self) -> str:
        """Get the configuration version."""
        return self._version

    @property
    def is_loaded(self) -> bool:
        """Check if profiles have been loaded."""
        return self._loaded

    def load(self) -> Dict[str, WorkloadProfile]:
        """
        Load profiles from the YAML configuration file.

        Returns:
            Dictionary of profile name -> WorkloadProfile.

        Raises:
            FileNotFoundError: If config file doesn't exist.
            yaml.YAMLError: If config file is invalid YAML.
        """
        if self._loaded:
            return self._profiles

        if yaml is None:
            raise RuntimeError("PyYAML is required to load workload profiles; install requirements.txt")

        if not os.path.exists(self._config_path):
            logger.warning(f"Profile config not found: {self._config_path}")
            # Return empty dict, caller should handle
            return self._profiles

        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse profile config: {e}")
            raise

        # Handle empty or None YAML data
        if data is None:
            data = {}

        # Load metadata
        self._version = data.get('version', '1.0')
        self._description = data.get('description', '')

        # Load common settings (use 'or {}' to handle null values like 'common:')
        self._common_settings = data.get('common') or {}

        # Load GPU profiles (use 'or {}' to handle null values like 'gpu:')
        gpu_section = data.get('gpu') or {}
        for name, profile_data in gpu_section.items():
            full_name = f"gpu_{name}"
            profile = self._parse_profile(
                full_name=full_name,
                target="gpu",
                data=profile_data
            )
            self._profiles[full_name] = profile

        # Load CPU profiles (use 'or {}' to handle null values like 'cpu:')
        cpu_section = data.get('cpu') or {}
        for name, profile_data in cpu_section.items():
            full_name = f"cpu_{name}"
            profile = self._parse_profile(
                full_name=full_name,
                target="cpu",
                data=profile_data
            )
            self._profiles[full_name] = profile

        self._loaded = True

        # Log summary
        gpu_count = len([p for p in self._profiles.values() if p.target == "gpu"])
        cpu_count = len([p for p in self._profiles.values() if p.target == "cpu"])
        pending_count = len([p for p in self._profiles.values() if p.is_pending])

        logger.info(
            f"Loaded {len(self._profiles)} workload profiles: "
            f"{gpu_count} GPU, {cpu_count} CPU, {pending_count} pending"
        )

        return self._profiles

    def _parse_profile(
        self,
        full_name: str,
        target: str,
        data: Dict[str, Any]
    ) -> WorkloadProfile:
        """
        Parse a single profile from YAML data.

        Args:
            full_name: Full profile name (e.g., "gpu_vulkan_game_light")
            target: Target type ("gpu" or "cpu")
            data: Profile data from YAML

        Returns:
            WorkloadProfile instance.
        """
        # Get common settings for defaults
        serial_defaults = self._common_settings.get('serial', {})
        test_defaults = self._common_settings.get('test', {})

        return WorkloadProfile(
            name=full_name,
            target=target,
            display_name=data.get('display_name', full_name),
            description=data.get('description', ''),
            workload_path=data.get('workload_path', ''),
            default_args=data.get('default_args', []),
            serial_device=data.get('serial_device', serial_defaults.get('device', '/dev/ttyAMA0')),
            expected_duration_sec=data.get('expected_duration_sec', 30),
            status=data.get('status', 'implemented'),
            notes=data.get('notes'),
            # Apply common defaults if not specified in profile
            baudrate=serial_defaults.get('baudrate', 115200),
            heartbeat_timeout_sec=test_defaults.get('heartbeat_timeout_sec', 45),
            grace_period_sec=test_defaults.get('grace_period_sec', 2)
        )

    def get(self, name: str) -> Optional[WorkloadProfile]:
        """
        Get a profile by name.

        Args:
            name: Profile name (e.g., "gpu_vulkan_game_light")

        Returns:
            WorkloadProfile if found, None otherwise.
        """
        if not self._loaded:
            self.load()
        return self._profiles.get(name)

    def list_all(self) -> List[WorkloadProfile]:
        """
        List all profiles.

        Returns:
            List of all WorkloadProfile objects.
        """
        if not self._loaded:
            self.load()
        return list(self._profiles.values())

    def list_gpu(self) -> List[WorkloadProfile]:
        """
        List GPU profiles.

        Returns:
            List of GPU WorkloadProfile objects.
        """
        if not self._loaded:
            self.load()
        return [p for p in self._profiles.values() if p.target == "gpu"]

    def list_cpu(self) -> List[WorkloadProfile]:
        """
        List CPU profiles.

        Returns:
            List of CPU WorkloadProfile objects (includes pending).
        """
        if not self._loaded:
            self.load()
        return [p for p in self._profiles.values() if p.target == "cpu"]

    def list_implemented(self) -> List[WorkloadProfile]:
        """
        List only implemented (available) profiles.

        Returns:
            List of implemented WorkloadProfile objects.
        """
        if not self._loaded:
            self.load()
        return [p for p in self._profiles.values() if p.is_implemented]

    def list_pending(self) -> List[WorkloadProfile]:
        """
        List only pending profiles.

        Returns:
            List of pending WorkloadProfile objects.
        """
        if not self._loaded:
            self.load()
        return [p for p in self._profiles.values() if p.is_pending]

    def list_available(self, include_deprecated: bool = False) -> List[WorkloadProfile]:
        """
        List available profiles for use.

        Args:
            include_deprecated: If True, include deprecated profiles.

        Returns:
            List of available WorkloadProfile objects.
        """
        if not self._loaded:
            self.load()

        profiles = [p for p in self._profiles.values() if p.is_available]

        if include_deprecated:
            profiles.extend([p for p in self._profiles.values() if p.is_deprecated])

        return profiles

    def reload(self) -> Dict[str, WorkloadProfile]:
        """
        Force reload of profiles from file.

        Returns:
            Dictionary of profile name -> WorkloadProfile.
        """
        self._loaded = False
        self._profiles.clear()
        return self.load()

    def get_by_target(self, target: str) -> List[WorkloadProfile]:
        """
        Get profiles by target type.

        Args:
            target: Target type ("gpu" or "cpu")

        Returns:
            List of matching WorkloadProfile objects.
        """
        if not self._loaded:
            self.load()
        return [p for p in self._profiles.values() if p.target == target]


class WorkloadProfileRegistry:
    """
    Singleton registry for workload profiles.

    Provides global access to profiles throughout the application.
    Thread-safe implementation using RLock to protect shared state.

    Usage:
        # Initialize once at application start
        registry = WorkloadProfileRegistry()
        registry.load("config/workload_profiles.yaml")

        # Access anywhere in the application
        profile = WorkloadProfileRegistry.get("gpu_vulkan_game_light")

        # Or use the shared instance
        profile = WorkloadProfileRegistry().get("gpu_vulkan_game_light")
    """

    # Class-level singleton state (protected by _singleton_lock)
    _instance: Optional["WorkloadProfileRegistry"] = None
    _singleton_lock = __import__('threading').RLock()

    def __new__(cls, config_path: str = None) -> "WorkloadProfileRegistry":
        """Thread-safe singleton creation."""
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                # Initialize instance-level state
                cls._instance._loader: Optional[WorkloadProfileLoader] = None
                cls._instance._profiles: Dict[str, WorkloadProfile] = {}
                cls._instance._loaded: bool = False
                cls._instance._lock = __import__('threading').RLock()
                # Initialize with config path if provided
                if config_path:
                    cls._instance._loader = WorkloadProfileLoader(config_path)
            return cls._instance

    def __init__(self, config_path: str = None):
        """
        Initialize registry.

        Args:
            config_path: Path to workload_profiles.yaml. If provided,
                        profiles are loaded immediately.
        Note:
            Due to singleton pattern, __init__ may be called multiple times
            with different config_path values. The first config_path is used.
        """
        # __new__ handles the actual initialization, __init__ is idempotent
        pass

    def load(self, config_path: str = None) -> None:
        """
        Load profiles from configuration file (thread-safe).

        Args:
            config_path: Optional path override. If provided, creates new loader.
        """
        with self._lock:
            if config_path and (self._loader is None or config_path != self._loader._config_path):
                self._loader = WorkloadProfileLoader(config_path)

            if self._loader:
                self._profiles = self._loader.load()
                self._loaded = True

    def get(self, name: str) -> Optional[WorkloadProfile]:
        """
        Get profile by name (thread-safe).

        Args:
            name: Profile name.

        Returns:
            WorkloadProfile if found, None otherwise.
        """
        with self._lock:
            if not self._loaded:
                self.load()
            return self._profiles.get(name)

    def list_all(self) -> List[WorkloadProfile]:
        """List all profiles (thread-safe)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return list(self._profiles.values())

    def list_gpu(self) -> List[WorkloadProfile]:
        """List GPU profiles (thread-safe)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return [p for p in self._profiles.values() if p.target == "gpu"]

    def list_cpu(self) -> List[WorkloadProfile]:
        """List CPU profiles (thread-safe)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return [p for p in self._profiles.values() if p.target == "cpu"]

    def list_implemented(self) -> List[WorkloadProfile]:
        """List only implemented profiles (thread-safe)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return [p for p in self._profiles.values() if p.is_implemented]

    def list_pending(self) -> List[WorkloadProfile]:
        """List only pending profiles (thread-safe)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return [p for p in self._profiles.values() if p.is_pending]

    def list_available(self) -> List[WorkloadProfile]:
        """List available profiles (implemented and not deprecated) (thread-safe)."""
        with self._lock:
            if not self._loaded:
                self.load()
            return [p for p in self._profiles.values() if p.is_available]

    @classmethod
    def reset(cls) -> None:
        """
        Reset the singleton instance (thread-safe).

        Primarily for testing purposes.
        """
        with cls._singleton_lock:
            if cls._instance is not None:
                instance = cls._instance
                cls._instance = None
                # Clear instance state
                instance._profiles.clear()
                instance._loader = None
                instance._loaded = False


def get_default_config_path() -> str:
    """
    Get the default profile configuration path.

    Returns:
        Path to config/workload_profiles.yaml relative to src directory.
    """
    # Get the directory containing this module
    module_dir = os.path.dirname(os.path.abspath(__file__))
    # Go up one level to project root, then to config
    project_root = os.path.dirname(module_dir)
    return os.path.join(project_root, "config", "workload_profiles.yaml")
