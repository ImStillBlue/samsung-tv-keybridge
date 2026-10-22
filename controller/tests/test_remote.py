import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

import anyio
import pytest
from anyio.abc import SocketStream, TaskGroup
from pydantic import ValidationError

from theatre.config import ControllerConfig, Mode
from theatre.remote import (
    InputEvent,
    ProtocolError,
    RemoteIdentity,
    encode_frame,
    remote_session,
    verify_frame,
)


async def line(stream: SocketStream) -> bytes:
    result = bytearray()
    with anyio.fail_after(2):
        while not result.endswith(b"\n"):
            result.extend(await stream.receive(1))
    return bytes(result)


def frame(token: bytes, nonce: bytes, payload: str) -> bytes:
    # Independent agent-side wire fixture, not the host encoder under test.
    digest = hmac.digest(token, nonce + b"\0" + payload.encode(), hashlib.sha256).hex()
    return f"{payload}\t{digest}\n".encode()


@dataclass(frozen=True, slots=True)
class LoopbackAgent:
    tasks: TaskGroup
    closed: anyio.Event
    mode: str
    payloads: tuple[str, ...] = ()
    corrupt_identity: bool = False

    async def stage_remote(self, port: int, token: bytes) -> None:
        self.tasks.start_soon(self.run, port, token)

    async def run(self, port: int, token: bytes) -> None:
        async with await anyio.connect_tcp("127.0.0.1", port) as stream:
            challenge = (await line(stream)).decode().rstrip("\n").split("\t")
            assert challenge[0] == "THEATRE-REMOTE/1"
            nonce = bytes.fromhex(challenge[1])
            identity = json.dumps({
                "process_id": 123, "user_id": 0, "group_id": 0,
                "device": "Smart Control 2016", "node": "/dev/input/event99",
                "bus": "0005", "vendor": "04e8", "product": "2075",
                "version": "0001", "mode": self.mode,
            })
            encoded = base64.b64encode(identity.encode()).decode()
            key = bytes(32) if self.corrupt_identity else token
            await stream.send(frame(key, nonce, f"IDENTITY\t{encoded}"))
            if not self.corrupt_identity:
                assert await line(stream) == frame(token, nonce, "HEARTBEAT\t1")
                ready = frame(token, nonce, "READY")
                # Deliberately fragment one frame and coalesce the following frames.
                await stream.send(ready[:3])
                await stream.send(ready[3:] + b"".join(
                    frame(token, nonce, item) for item in self.payloads
                ))
            try:
                while True:
                    _ = await stream.receive()
            except anyio.EndOfStream:
                self.closed.set()


@pytest.mark.anyio
async def test_loopback_handshake_fragmentation_and_shutdown(config: ControllerConfig) -> None:
    # Given a real callback peer with fragmented/coalesced managed protocol frames.
    closed = anyio.Event()
    async with anyio.create_task_group() as tasks:
        agent = LoopbackAgent(tasks, closed, "observe", (
            "EVENT\t1\t1\t63\t1\t0", "EVENT\t2\t1\t63\t0\t0",
        ))
        # When the host authenticates and reads those frames.
        with anyio.fail_after(2):
            async with remote_session(config, agent) as session:
                assert (await session.next_event()).value == 1
                assert (await session.next_event()).sequence == 2
            # Then close is observed on the actual socket, without polling/sleep.
            await closed.wait()
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_wrong_authentication_never_reaches_ready(config: ControllerConfig) -> None:
    # Given a peer with a wrong authentication token.
    closed = anyio.Event()
    async with anyio.create_task_group() as tasks:
        agent = LoopbackAgent(tasks, closed, "observe", corrupt_identity=True)
        # When it attempts the handshake.
        with anyio.fail_after(2), pytest.raises(ProtocolError):
            async with remote_session(config, agent):
                pytest.fail("untrusted peer reached READY")
        # Then its real accepted socket is closed.
        with anyio.fail_after(2):
            await closed.wait()
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_replayed_event_closes_loopback_session(config: ControllerConfig) -> None:
    # Given a valid agent replaying an authenticated input sequence.
    closed = anyio.Event()
    async with anyio.create_task_group() as tasks:
        agent = LoopbackAgent(tasks, closed, "observe", (
            "EVENT\t1\t1\t63\t1\t0", "EVENT\t1\t1\t63\t1\t0",
        ))
        # When the replay reaches the host.
        async def consume_replay() -> None:
            async with remote_session(config, agent) as session:
                _ = await session.next_event()
                _ = await session.next_event()

        with anyio.fail_after(2), pytest.raises(ExceptionGroup) as failure:
            await consume_replay()
        # Then authentication alone cannot bypass sequence validation.
        assert failure.value.subgroup(ProtocolError) is not None
        with anyio.fail_after(2):
            await closed.wait()
        tasks.cancel_scope.cancel()


@pytest.mark.parametrize("changed", [b"wrong token", b"wrong nonce"])
def test_frame_authentication_binds_token_and_nonce(changed: bytes) -> None:
    # Given a real managed-compatible HMAC frame.
    token, nonce = b"t" * 32, b"n" * 32
    encoded = frame(token, nonce, "READY")
    # When different authentication material is supplied.
    replacement = changed.ljust(32, b"\0")
    with pytest.raises(ProtocolError):
        _ = verify_frame(replacement, nonce, encoded)
    # Then nonce changes are also rejected.
    with pytest.raises(ProtocolError):
        _ = verify_frame(token, replacement, encoded)


@pytest.mark.parametrize("payload", [
    "EVENT\t01\t1\t63\t1\t0", "EVENT\t1\t1\t63\t3\t0",
    "EVENT\t1\t1\t63\t1\t1", "READY", "EVENT\t1\t65536\t0\t0\t0",
])
def test_invalid_event_is_rejected(payload: str) -> None:
    # Given an authenticated but invalid event boundary.
    # When parsing for map mode.
    with pytest.raises(ProtocolError):
        _ = InputEvent.parse(payload, 0, Mode.MAP)
    # Then no dispatchable value was produced.


@pytest.mark.parametrize("line_bytes", [b"", b"READY", b"x" * 4096 + b"\n", b"\xff\n"])
def test_invalid_framing_is_rejected(line_bytes: bytes) -> None:
    # Given incomplete, oversized or non-UTF8 wire data.
    # When verifying it.
    with pytest.raises(ProtocolError):
        _ = verify_frame(b"t" * 32, b"n" * 32, line_bytes)
    # Then no payload is accepted.


def test_managed_wire_fixture_matches_host_encoder() -> None:
    # Given independent implementations of the managed wire contract.
    token, nonce = b"t" * 32, b"n" * 32
    # When encoding the first heartbeat.
    encoded = encode_frame(token, nonce, "HEARTBEAT\t1")
    # Then bytes match exactly.
    assert encoded == frame(token, nonce, "HEARTBEAT\t1")


@pytest.mark.parametrize(("field", "value"), [
    ("user_id", 1), ("group_id", 1), ("device", "theatre-forwarded-remote"),
    ("bus", "0006"), ("vendor", "0000"), ("product", "0000"),
])
def test_untrusted_identity_is_rejected(field: str, value: str | int) -> None:
    # Given an identity JSON from a peer which must never be grabbed.
    identity: dict[str, str | int] = {
        "process_id": 123, "user_id": 0, "group_id": 0,
        "device": "Smart Control 2016", "node": "/dev/input/event1",
        "bus": "0005", "vendor": "04e8", "product": "2075",
        "version": "0001", "mode": "map",
    }
    identity[field] = value
    # When parsing the boundary.
    with pytest.raises(ValidationError):
        _ = RemoteIdentity.model_validate_json(json.dumps(identity))
    # Then it cannot reach READY.
