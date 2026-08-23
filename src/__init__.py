"""
Vmin Judge Tool - Python PC-side Implementation

A real-time log monitoring and pass/fail judgment system for Vmin
(Voltage Minimum) testing on embedded devices.

Architecture: 5-layer design (see docs/ARCHITECTURE.md)

Testing Strategy:
- Unit tests use mocks (fast, no hardware needed)
- Integration tests use real hardware (--run-real flag)
- See docs/TESTING_STRATEGY.md for details
"""

__version__ = "2.0.0"
__author__ = "Vmin Judge Tool Development"

from .channel_manager import (
    DeviceChannel,
    HDCChannel,
    ADBChannel,
    ChannelManager,
    ChannelState,
    ChannelHealth,
    InvokeResult,
    create_channel_manager
)

from .serial_monitor import (
    SerialMonitor,
    SerialMonitorError,
    SerialPortNotFoundError,
    SerialReadError,
    SerialConfig,
    MockSerialMonitor,
    list_available_ports
)

from .log_parser import (
    LogParser,
    ParsedLine,
    ParseConfig,
    StreamingLogParser,
    DMESG_TIMESTAMP_PATTERN,
    WORKLOAD_PATTERNS
)

from .serial_port_manager import (
    PCSerialPort,
    DeviceSerialPort,
    PortPair,
    PairingResult,
    SerialPortConfig,
    PCSerialScanner,
    DeviceSerialScanner,
    SerialPairingEngine,
    SerialPortManager,
    RealPCSerialScanner,
    MockPCSerialScanner,
    RealDeviceSerialScanner,
    MockDeviceSerialScanner,
    RealSerialPairingEngine,
    MockSerialPairingEngine,
    create_serial_port_manager
)

from .workload_profiles import (
    WorkloadProfile,
    WorkloadProfileLoader,
    WorkloadProfileRegistry,
    get_default_config_path
)

from .workload_builder import (
    SerialRedirectCommandBuilder,
    WorkloadCommandBuilder,
    WorkloadCommandConfig,
    create_workload_command_builder
)

from .test_orchestrator import (
    TestOrchestratorConfig,
    TestSession,
    PrepareResult,
    ExecutionResult,
    RunMode,
    LogChannel,
    create_test_orchestrator
)
from .artifact_store import ArtifactStore
from .baselines import Baseline, BaselineRegistry
from .config_loader import PlatformConfig, ProfileConfig
from .deployment import AssetSpec, DeploymentManager
from .events import EventDecoder, EventEnvelope
from .path_resolver import PathResolver
from .platform_probe import PlatformProbe
from .policy_engine import PolicyEngine, PolicyLimits, RunExitCode
from .qualification import CalibrationService, GoldenService
from .run_orchestrator import RunManifestBuilder, RunOrchestrator
from .transport import ADBTransport, HDCTransport, TransportManager
# Note: TestOrchestrator class does not exist in test_orchestrator.py
# Only TestOrchestratorConfig, TestSession, etc. are available

__all__ = [
    # L1: Communication
    "DeviceChannel",
    "HDCChannel",
    "ADBChannel",
    "ChannelManager",
    "ChannelState",
    "ChannelHealth",
    "InvokeResult",
    "create_channel_manager",
    # L2: Ingestion
    "SerialMonitor",
    "SerialMonitorError",
    "SerialPortNotFoundError",
    "SerialReadError",
    "SerialConfig",
    "MockSerialMonitor",
    "list_available_ports",
    "LogParser",
    "ParsedLine",
    "ParseConfig",
    "StreamingLogParser",
    "DMESG_TIMESTAMP_PATTERN",
    "WORKLOAD_PATTERNS",
    # L1-L2: Serial Port Management
    "PCSerialPort",
    "DeviceSerialPort",
    "PortPair",
    "PairingResult",
    "SerialPortConfig",
    "PCSerialScanner",
    "DeviceSerialScanner",
    "SerialPairingEngine",
    "SerialPortManager",
    "RealPCSerialScanner",
    "MockPCSerialScanner",
    "RealDeviceSerialScanner",
    "MockDeviceSerialScanner",
    "RealSerialPairingEngine",
    "MockSerialPairingEngine",
    "create_serial_port_manager",
    # Stage 4: Workload Profiles & Orchestration
    "WorkloadProfile",
    "WorkloadProfileLoader",
    "WorkloadProfileRegistry",
    "get_default_config_path",
    "SerialRedirectCommandBuilder",
    "WorkloadCommandBuilder",
    "WorkloadCommandConfig",
    "create_workload_command_builder",
    "TestOrchestratorConfig",
    "TestSession",
    "PrepareResult",
    "ExecutionResult",
    "RunMode",
    "LogChannel",
    "create_test_orchestrator",
    # V2 target contracts
    "PathResolver",
    "PlatformConfig",
    "ProfileConfig",
    "EventEnvelope",
    "EventDecoder",
    "PolicyEngine",
    "PolicyLimits",
    "RunExitCode",
    "ArtifactStore",
    "Baseline",
    "BaselineRegistry",
    "GoldenService",
    "CalibrationService",
    "PlatformProbe",
    "AssetSpec",
    "DeploymentManager",
    "ADBTransport",
    "HDCTransport",
    "TransportManager",
    "RunManifestBuilder",
    "RunOrchestrator",
]
