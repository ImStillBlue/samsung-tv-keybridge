import logging
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from ipaddress import IPv4Address
from typing import Final

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketListener, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.memory import MemoryObjectSendStream

from theatre.bridge import BridgeConfig, run_bridge

LOOPBACK: Final = IPv4Address("127.0.0.1")


@dataclass(frozen=True, slots=True)
class BridgeReady:
    ports: MemoryObjectSendStream[int]

    def started(self, value: int | None = None) -> None:
        assert value is not None
        self.ports.send_nowait(value)


@asynccontextmanager
async def bridge_server(
    target_port: int, *, clients: tuple[IPv4Address, ...] = (LOOPBACK,),
    max_connections: int = 4,
) -> AsyncIterator[int]:
    with anyio.fail_after(3):
        send, receive = anyio.create_memory_object_stream[int](1)
        async with anyio.create_task_group() as tasks, send, receive:
            config = BridgeConfig(
                callback_host=LOOPBACK, server_host=LOOPBACK,
                clients=clients,
                listen_port=0, target_port=target_port, max_connections=max_connections,
            )
            tasks.start_soon(partial(run_bridge, config, task_status=BridgeReady(send)))
            port = await receive.receive()
            try:
                yield port
            finally:
                tasks.cancel_scope.cancel()


@pytest.mark.anyio
@pytest.mark.parametrize("close_from_server", [False, True])
async def test_allowlisted_bytes_flow_both_ways_and_either_close_ends_session(
    *, close_from_server: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a real loopback SDB server and an allowlisted bridge.
    received: list[bytes] = []
    finish = anyio.Event()
    server_closed = anyio.Event()
    payload = bytes(range(256)) * 1024
    caplog.set_level(logging.INFO, logger="theatre")

    async def server(stream: SocketStream) -> None:
        async with stream:
            buffered = BufferedByteReceiveStream(stream)
            received.append(await buffered.receive_exactly(len(payload)))
            await stream.send(payload[::-1])
            if close_from_server:
                await finish.wait()
            else:
                with pytest.raises(anyio.EndOfStream):
                    _ = await stream.receive()
        server_closed.set()

    async with (
        await anyio.create_tcp_listener(local_host="127.0.0.1") as listener,
        anyio.create_task_group() as tasks,
    ):
        tasks.start_soon(listener.serve, server)
        async with bridge_server(listener.extra(attribute=SocketAttribute.local_port)) as port:
            async with await anyio.connect_tcp("127.0.0.1", port) as client:
                # When arbitrary binary data is exchanged and one side closes.
                await client.send(payload)
                assert await BufferedByteReceiveStream(client).receive_exactly(
                    len(payload),
                ) == payload[::-1]
                if close_from_server:
                    finish.set()
                    with pytest.raises(anyio.EndOfStream):
                        _ = await client.receive()
            await server_closed.wait()
        tasks.cancel_scope.cancel()
    # Then both streams close and the successful session is logged.
    assert received == [payload]
    assert "bridge.client peer=127.0.0.1" in caplog.text
    assert "bridge.closed peer=127.0.0.1" in caplog.text


@pytest.mark.anyio
async def test_unlisted_client_closes_without_contacting_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a listening SDB server but a different allowlisted client.
    connections: list[str] = []

    async def server(stream: SocketStream) -> None:
        async with stream:
            connections.append(stream.extra(attribute=SocketAttribute.remote_address)[0])
            await stream.send(b"accepted")

    async with (
        await anyio.create_tcp_listener(local_host="127.0.0.1") as listener,
        anyio.create_task_group() as tasks,
    ):
        tasks.start_soon(listener.serve, server)
        target_port = listener.extra(attribute=SocketAttribute.local_port)
        async with bridge_server(target_port, clients=(IPv4Address("127.0.0.2"),)) as port:
            # When an unlisted peer connects, Then it receives EOF immediately.
            async with await anyio.connect_tcp("127.0.0.1", port) as client:
                with pytest.raises(anyio.EndOfStream):
                    _ = await client.receive()
            # Fence the server accept queue with a direct connection, not a timed absence check.
            async with await anyio.connect_tcp("127.0.0.1", target_port) as sentinel:
                assert await sentinel.receive() == b"accepted"
        tasks.cancel_scope.cancel()
    assert connections == ["127.0.0.1"]
    assert "bridge.rejected peer=127.0.0.1" in caplog.text


@pytest.mark.anyio
async def test_unreachable_server_closes_client(caplog: pytest.LogCaptureFixture) -> None:
    # Given a reserved local port with no listener.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        async with (
            await SocketListener.from_socket(reserved) as bound,
            bridge_server(bound.extra(attribute=SocketAttribute.local_port)) as port,
            await anyio.connect_tcp("127.0.0.1", port) as client,
        ):
            # When the allowed client connects, Then failed server connection becomes EOF.
            with pytest.raises(anyio.EndOfStream):
                _ = await client.receive()
    assert "bridge.server_unreachable peer=127.0.0.1" in caplog.text


@pytest.mark.anyio
async def test_connection_limit_rejects_excess_and_releases_capacity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given one allowed active session occupying the configured capacity.
    disconnected = anyio.Event()
    close_first = anyio.Event()
    connections: list[str] = []

    async def server(stream: SocketStream) -> None:
        async with stream:
            connections.append(stream.extra(attribute=SocketAttribute.remote_address)[0])
            await stream.send(b"ready")
            if len(connections) == 1:
                await close_first.wait()
            else:
                with pytest.raises(anyio.EndOfStream):
                    _ = await stream.receive()
        disconnected.set()

    async with (
        await anyio.create_tcp_listener(local_host="127.0.0.1") as listener,
        anyio.create_task_group() as tasks,
    ):
        tasks.start_soon(listener.serve, server)
        async with (
            bridge_server(
                listener.extra(attribute=SocketAttribute.local_port), max_connections=1,
            ) as port,
            await anyio.connect_tcp("127.0.0.1", port) as first,
        ):
            assert await first.receive() == b"ready"
            # When capacity is exhausted, Then the extra client is closed.
            async with await anyio.connect_tcp("127.0.0.1", port) as extra:
                with pytest.raises(anyio.EndOfStream):
                    _ = await extra.receive()
            assert len(connections) == 1
            close_first.set()
            with pytest.raises(anyio.EndOfStream):
                _ = await first.receive()
            await disconnected.wait()
            async with await anyio.connect_tcp("127.0.0.1", port) as replacement:
                assert await replacement.receive() == b"ready"
            assert len(connections) == 2
        tasks.cancel_scope.cancel()
    assert "bridge.capacity peer=127.0.0.1" in caplog.text


@pytest.mark.anyio
async def test_cancellation_closes_active_client_and_server_streams() -> None:
    # Given an active relay with both sockets open.
    server_closed = anyio.Event()

    async def server(stream: SocketStream) -> None:
        async with stream:
            await stream.send(b"ready")
            with pytest.raises(anyio.EndOfStream):
                _ = await stream.receive()
        server_closed.set()

    async with (
        await anyio.create_tcp_listener(local_host="127.0.0.1") as listener,
        anyio.create_task_group() as tasks,
        AsyncExitStack() as clients,
    ):
        tasks.start_soon(listener.serve, server)
        async with bridge_server(listener.extra(attribute=SocketAttribute.local_port)) as port:
            client = await clients.enter_async_context(await anyio.connect_tcp("127.0.0.1", port))
            assert await client.receive() == b"ready"
            # When the bridge scope exits, all active relay tasks are cancelled.
        # Then both the PC and fake server observe EOF before cleanup returns.
        with anyio.fail_after(2):
            with pytest.raises(anyio.EndOfStream):
                _ = await client.receive()
            await server_closed.wait()
        tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_bind_failure_is_not_swallowed() -> None:
    # Given an occupied interface/port.
    async with await anyio.create_tcp_listener(local_host="127.0.0.1") as listener:
        # When binding the bridge, Then its OS error remains visible.
        with pytest.raises(OSError, match=r"\[Errno 98\]"):
            await run_bridge(BridgeConfig(
                callback_host=LOOPBACK,
                clients=(IPv4Address("127.0.0.1"),),
                listen_port=listener.extra(attribute=SocketAttribute.local_port),
            ))


def test_bridge_defaults_to_owned_server_endpoint() -> None:
    # Given a callback interface and trusted peer.
    config = BridgeConfig(callback_host=LOOPBACK, clients=(LOOPBACK,))
    # When defaults are used, Then clients share the local server, not TV SDB.
    assert config.listen_port == 26098
    assert config.server_host == LOOPBACK
    assert config.target_port == 26099
