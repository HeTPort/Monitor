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

__version__ = "2.0.1"
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
