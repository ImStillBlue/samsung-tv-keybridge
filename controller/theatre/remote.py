"""Exact THEATRE-REMOTE/1 wire contract and ordered connection ownership."""

import base64
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, ClassVar, Final, Literal, Protocol, final

import anyio
from anyio.abc import SocketAttribute, SocketListener, SocketStream
from pydantic import BaseModel, ConfigDict, Field

from theatre.config import (
    HEARTBEAT_SECONDS,
    RELEASE_SECONDS,
    TOKEN_BYTES,
    ControllerConfig,
    ControllerError,
    Mode,
    Operation,
    RemoteName,
    RootLostError,
)
from theatre.keys import SAFE_KEYS

MAXIMUM_FRAME_BYTES: Final = 4096
MAX_SEQUENCE: Final = (1 << 63) - 1
MAX_EVENT_CODE: Final = (1 << 16) - 1
EVENT_FIELDS: Final = 6
IDENTITY_FIELDS: Final = 2
SCAN_TYPE: Final = 4
SCAN_CODE: Final = 4


class ProtocolError(ControllerError):
    """An input session is no longer trustworthy and must be closed."""


def encode_frame(token: bytes, nonce: bytes, payload: str) -> bytes:
    """Authenticate nonce + NUL + UTF-8 payload, as the managed agent does."""
    if len(token) != TOKEN_BYTES or len(nonce) != TOKEN_BYTES or "\r" in payload or "\n" in payload:
        raise ProtocolError(Operation.FRAME_MATERIAL)
    digest = hmac.new(token, nonce + b"\0" + payload.encode(), hashlib.sha256).hexdigest()
    frame = f"{payload}\t{digest}\n".encode()
    if len(frame) > MAXIMUM_FRAME_BYTES:
        raise ProtocolError(Operation.FRAME_SIZE)
    return frame


def verify_frame(token: bytes, nonce: bytes, line: bytes) -> str:
    """Verify a complete, bounded frame without accepting embedded delimiters."""
    if not line.endswith(b"\n") or not 0 < len(line) <= MAXIMUM_FRAME_BYTES:
        raise ProtocolError(Operation.FRAME_SIZE)
    try:
        payload, observed = line.decode("utf-8").rstrip("\r\n").rsplit("\t", 1)
    except (UnicodeDecodeError, ValueError) as error:
        raise ProtocolError(Operation.FRAME_ENCODING) from error
    expected = encode_frame(token, nonce, payload).rsplit(b"\t", 1)[1].rstrip(b"\n")
    if not hmac.compare_digest(observed.encode(), expected):
        raise ProtocolError(Operation.FRAME_HMAC)
    return payload


class RemoteIdentity(BaseModel):
    """Authenticated physical identity; required capabilities are agent-verified."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", strict=True, hide_input_in_errors=True
    )
    process_id: Annotated[int, Field(gt=0, le=2147483647)]
    user_id: Literal[0]
    group_id: Literal[0]
    device: RemoteName
    node: Annotated[str, Field(pattern=r"^/dev/input/event[0-9]+$")]
    bus: Literal["0005"]
    vendor: Literal["04e8"]
    product: Annotated[str, Field(pattern=r"^[0-9a-f]{4}$")]
    version: Annotated[str, Field(pattern=r"^[0-9a-f]{4}$")]
    mode: Literal["observe", "forward", "map"]


@dataclass(frozen=True, slots=True)
class InputEvent:
    """One authenticated kernel event with an independent replay sequence."""

    sequence: int
    type: int
    code: int
    value: int
    forwarded: bool

    @classmethod
    def parse(
        cls, payload: str, previous: int, mode: Mode, keys: frozenset[int]
    ) -> "InputEvent":
        """Parse numeric bounds, replay ordering and mode-consistent forwarding."""
        fields = payload.split("\t")
        if len(fields) != EVENT_FIELDS or fields[0] != "EVENT":
            raise ProtocolError(Operation.EVENT_MESSAGE)
        if not all(re.fullmatch(r"0|[1-9][0-9]*", text) for text in fields[1:4]):
            raise ProtocolError(Operation.EVENT_NUMBER)
        if not re.fullmatch(r"0|-?[1-9][0-9]*", fields[4]) or fields[5] not in {"0", "1"}:
            raise ProtocolError(Operation.EVENT_VALUE)
        sequence, event_type, code, value = map(int, fields[1:5])
        if (
            not previous < sequence <= MAX_SEQUENCE
            or (previous == 0 and sequence != 1)
            or not 0 <= event_type <= MAX_EVENT_CODE
            or not 0 <= code <= MAX_EVENT_CODE
            or not -(1 << 31) <= value < (1 << 31)
            or (event_type == 1 and value not in {0, 1, 2})
        ):
            raise ProtocolError(Operation.EVENT_SEQUENCE)
        forwarded = fields[5] == "1"
        match mode:
            case Mode.OBSERVE:
                valid = not forwarded
            case Mode.FORWARD:
                valid = forwarded
            case Mode.MAP:
                valid = (event_type == SCAN_TYPE and code == SCAN_CODE) or (
                    forwarded != (event_type == 1 and code in keys)
                )
        if not valid:
            raise ProtocolError(Operation.EVENT_FORWARDING)
        return cls(sequence, event_type, code, value, forwarded)


class AgentStarter(Protocol):
    """Only the staging capability needed by the remote callback."""

    async def stage_remote(self, port: int, token: bytes, keys: frozenset[int]) -> None:
        """Start the private managed agent after its listener is bound."""
        ...


@final
class RemoteSession:
    """Mutable frame buffer, counters and accepted-socket ownership."""

    def __init__(
        self, config: ControllerConfig, listener: SocketListener, token: bytes
    ) -> None:
        """Own one callback socket and its authenticated framing state."""
        self._config = config
        self._listener = listener
        self._token = token
        self._nonce = secrets.token_bytes(TOKEN_BYTES)
        self._stream: SocketStream | None = None
        self._buffer = bytearray()
        self._sequence = 0
        self._heartbeat_sequence = 0
        self._closed = False

    async def _line(self) -> bytes:
        stream = self._connected()
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                if end + 1 > MAXIMUM_FRAME_BYTES:
                    raise ProtocolError(Operation.FRAME_SIZE)
                result = bytes(self._buffer[:end + 1])
                del self._buffer[:end + 1]
                return result
            if len(self._buffer) >= MAXIMUM_FRAME_BYTES:
                raise ProtocolError(Operation.FRAME_SIZE)
            self._buffer.extend(await stream.receive(MAXIMUM_FRAME_BYTES))

    def _connected(self) -> SocketStream:
        if self._stream is None:
            raise ProtocolError(Operation.SESSION_UNCONNECTED)
        return self._stream

    async def start(self) -> RemoteIdentity:
        """Accept only the TV, then authenticate identity before enabling health."""
        with anyio.fail_after(self._config.accept_timeout):
            while self._stream is None:
                candidate = await self._listener.accept()
                peer_host = candidate.extra(attribute=SocketAttribute.remote_address)[0]
                if peer_host != str(self._config.tv_host):
                    await candidate.aclose()
                    continue
                self._stream = candidate
            with anyio.fail_after(RELEASE_SECONDS):
                await self._connected().send(
                    f"THEATRE-REMOTE/1\t{self._nonce.hex()}\n".encode()
                )
                identity = verify_frame(self._token, self._nonce, await self._line())
                fields = identity.split("\t")
                if len(fields) != IDENTITY_FIELDS or fields[0] != "IDENTITY":
                    raise ProtocolError(Operation.IDENTITY_REQUIRED)
                try:
                    parsed = RemoteIdentity.model_validate_json(
                        base64.b64decode(fields[1], validate=True)
                    )
                except ValueError as error:
                    raise ProtocolError(Operation.IDENTITY_INVALID) from error
                if parsed.mode != self._config.mode.value:
                    raise ProtocolError(Operation.MODE_MISMATCH)
                await self.send_heartbeat()
                if verify_frame(self._token, self._nonce, await self._line()) != "READY":
                    raise ProtocolError(Operation.READY_REQUIRED)
                return parsed

    async def send_heartbeat(self) -> None:
        """Advance the host-only heartbeat sequence with a bounded socket write."""
        self._heartbeat_sequence += 1
        with anyio.fail_after(HEARTBEAT_SECONDS):
            await self._connected().send(
                encode_frame(self._token, self._nonce, f"HEARTBEAT\t{self._heartbeat_sequence}")
            )

    async def heartbeats(self) -> None:
        """Keep health independent of input traffic and native launch latency."""
        timer = anyio.Event()
        while True:
            with anyio.move_on_after(HEARTBEAT_SECONDS):
                await timer.wait()
            await self.send_heartbeat()

    async def next_event(self) -> InputEvent:
        """Read exactly one authenticated event, rejecting replay before dispatch."""
        payload = verify_frame(self._token, self._nonce, await self._line())
        event = InputEvent.parse(
            payload, self._sequence, self._config.mode,
            frozenset(SAFE_KEYS[m.key] for m in self._config.mappings),
        )
        self._sequence = event.sequence
        return event

    async def close(self) -> None:
        """Request EOF, await the agent's cleanup/EOF, then close the listener."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._stream is not None:
                try:
                    await self._stream.send_eof()
                    with anyio.fail_after(RELEASE_SECONDS + 1):
                        while True:
                            _ = await self._stream.receive(MAXIMUM_FRAME_BYTES)
                except (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
                    pass
                except TimeoutError:
                    raise RootLostError(Operation.INPUT_RELEASE_TIMEOUT) from None
                finally:
                    await self._stream.aclose()
        finally:
            await self._listener.aclose()


@asynccontextmanager
async def remote_session(
    config: ControllerConfig, lease: AgentStarter
) -> AsyncIterator[RemoteSession]:
    """Bind before staging; own heartbeat cancellation and fail-open teardown."""
    listener = await anyio.create_tcp_listener(local_host=str(config.callback_host), local_port=0)
    token = secrets.token_bytes(TOKEN_BYTES)
    socket_listener = next(
        child for child in listener.listeners if isinstance(child, SocketListener)
    )
    session = RemoteSession(config, socket_listener, token)
    try:
        port = listener.extra(attribute=SocketAttribute.local_port)
        await lease.stage_remote(port, token, frozenset(SAFE_KEYS[m.key] for m in config.mappings))
        identity = await session.start()
        logging.getLogger("theatre").info(
            "remote.selected device=%s product=%s node=%s",
            identity.device, identity.product, identity.node,
        )
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(session.heartbeats)
            try:
                yield session
            finally:
                tasks.cancel_scope.cancel()
                with anyio.CancelScope(shield=True):
                    await session.close()
    finally:
        with anyio.CancelScope(shield=True):
            await session.close()
