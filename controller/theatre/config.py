"""Strict host configuration with explicitly selected safe application routes."""

from enum import StrEnum
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, ClassVar, Final, NewType, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from theatre.keys import KeyName

AppId = NewType("AppId", str)
APP_ID_PATTERN: Final = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+$"
MAX_APP_ID_LENGTH: Final = 128
RemoteName = Annotated[
    str, Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$")
]
HEARTBEAT_SECONDS: Final = 2.0
RELEASE_SECONDS: Final = 8.0
FORWARD_SECONDS: Final = 30.0
TOKEN_BYTES: Final = 32
MAX_PROCESS_ID: Final = (1 << 31) - 1
DEFAULT_STATE_DIRECTORY: Final = Path.home() / ".local" / "state" / "theatre"
BROADCAST_ADDRESS: Final = IPv4Address("255.255.255.255")


class Operation(StrEnum):
    """Stable, non-secret operational failures shared across trust boundaries."""

    APP_INVENTORY = "application-inventory"
    APP_FORBIDDEN = "unpermitted-application"
    LAUNCH_TRANSPORT = "native-launch-transport"
    LAUNCH_FAILED = "native-launch-failed"
    REMOTE_START = "input-agent-start"
    ROOT_CAPABILITIES = "archive-capabilities"
    ROOT_RUNTIME = "samsung-runtime"
    ROOT_TAR = "archive-tar-support"
    ROOT_OPT_IN = "root-opt-in-required"
    ROOT_IDENTITY = "root-identity"
    ROOT_CLEANUP = "owned-bootstrap-cleanup"
    SYSTEM_HASH = "system-hash-preservation"
    ROOT_TRANSPORT = "root-transport-lost"
    PAYLOAD_MISSING = "payload-missing"
    TARGETS_MISSING = "required-application-missing"
    TV_STANDBY = "tv-off-or-unreachable"
    TRIAL_CONFIG = "bounded-map-trial-required"
    FRAME_MATERIAL = "invalid-frame-material"
    FRAME_SIZE = "incomplete-or-oversized-frame"
    FRAME_ENCODING = "malformed-frame"
    FRAME_HMAC = "frame-authentication"
    EVENT_MESSAGE = "unexpected-input-message"
    EVENT_NUMBER = "noncanonical-input-number"
    EVENT_VALUE = "invalid-event-value"
    EVENT_SEQUENCE = "invalid-or-replayed-event"
    EVENT_FORWARDING = "inconsistent-forwarding-decision"
    SESSION_UNCONNECTED = "input-session-not-connected"
    IDENTITY_REQUIRED = "expected-input-identity"
    IDENTITY_INVALID = "invalid-input-identity"
    MODE_MISMATCH = "unexpected-input-mode"
    READY_REQUIRED = "expected-input-readiness"
    INPUT_RELEASE_TIMEOUT = "input-release-not-confirmed"
    STATE_INVALID = "invalid-bootstrap-ledger"
    STATE_PERMISSIONS = "insecure-controller-state-directory"
    CONTROLLER_RUNNING = "controller-already-running"


class ControllerError(RuntimeError):
    """An operational failure with a non-secret, typed operation identifier."""

    def __init__(self, operation: Operation) -> None:
        """Keep error output limited to a known operation code."""
        self.operation: Operation = operation
        super().__init__(operation.value)


class RootLostError(ControllerError):
    """Loss of an established control transport permits an opted-in reconnect."""

    def __init__(self, operation: Operation = Operation.ROOT_TRANSPORT) -> None:
        """Distinguish transport recovery from terminal launch/config failures."""
        super().__init__(operation)


class TelevisionUnavailableError(ControllerError):
    """No SDK/root action is allowed without an explicit powered-on response."""

    def __init__(self) -> None:
        """Fail closed when read-only power status is unavailable or off."""
        super().__init__(Operation.TV_STANDBY)


class SessionPolicyError(ValueError):
    """An unbounded forwarding test is not permitted."""

    def __init__(self) -> None:
        """Keep forwarding bounded independently of non-grabbing discovery."""
        super().__init__("forward mode requires bounded duration and no reconnect")


class MappingPolicyError(ValueError):
    """Mappings must be unique and used only in map mode."""

    def __init__(self) -> None:
        """Explain explicit setup requirements."""
        super().__init__("map mode requires mappings with unique keys; other modes require none")


class KeyMapping(BaseModel):
    """A verified key name mapped to one canonical Tizen application ID."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", strict=True, hide_input_in_errors=True
    )
    key: KeyName
    app_id: Annotated[AppId, Field(pattern=APP_ID_PATTERN, max_length=MAX_APP_ID_LENGTH)]


class AddressPolicyError(ValueError):
    """A callback must bind a specific unicast host, not every interface."""

    def __init__(self) -> None:
        """Reject addresses that cannot identify the TV/controller peers."""
        super().__init__("TV and callback addresses must identify unicast hosts")


class Mode(StrEnum):
    """Manual input-agent modes; observation is the conservative default."""

    OBSERVE = "observe"
    FORWARD = "forward"
    MAP = "map"


class ControllerConfig(BaseModel):
    """Frozen configuration parsed once at the host boundary.

    Addresses have no defaults: a config cannot accidentally target a known TV.
    Root acquisition and reconnect are separately opt-in, even for observation.
    The 2/8 second health values are agent constants, not measured hardware claims.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", strict=True, hide_input_in_errors=True
    )

    tv_host: IPv4Address
    callback_host: IPv4Address
    sdb: Path
    payloads: Path = Path(__file__).resolve().parents[1] / "build" / "payloads"
    state_dir: Path = DEFAULT_STATE_DIRECTORY
    allow_root: bool = False
    mode: Mode = Mode.OBSERVE
    reconnect: bool = False
    accept_timeout: Annotated[float, Field(gt=0, le=60)] = 20.0
    command_timeout: Annotated[float, Field(ge=0.1, le=30)] = 5.0
    reconnect_delay: Annotated[float, Field(gt=0, le=60)] = 5.0
    duration: Annotated[float, Field(gt=0, le=FORWARD_SECONDS)] | None = FORWARD_SECONDS
    remote_name: RemoteName | None = None
    mappings: tuple[KeyMapping, ...] = ()

    @model_validator(mode="after")
    def bounded_diagnostics(self) -> Self:
        """Bound forwarding and require explicit, unique map routes."""
        if self.mode == Mode.FORWARD and (self.duration is None or self.reconnect):
            raise SessionPolicyError
        if (
            (self.mode == Mode.MAP) != bool(self.mappings)
            or len({mapping.key for mapping in self.mappings}) != len(self.mappings)
        ):
            raise MappingPolicyError
        if any(
            address.is_unspecified or address.is_multicast or address == BROADCAST_ADDRESS
            for address in (self.tv_host, self.callback_host)
        ):
            raise AddressPolicyError
        return self
