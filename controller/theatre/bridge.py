"""Allowlisted developer TCP transport, independent of controller and root state."""

import logging
from ipaddress import IPv4Address
from typing import Annotated, ClassVar, Final

import anyio
from anyio.abc import SocketAttribute, SocketStream, TaskStatus
from pydantic import BaseModel, ConfigDict, Field

DEVELOPER_PORT: Final = 26098
logger: Final = logging.getLogger("theatre")


class BridgeConfig(BaseModel):
    """Frozen relay endpoints and resource bounds, injectable for loopback tests."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", strict=True)
    server_host: IPv4Address = IPv4Address("127.0.0.1")
    callback_host: IPv4Address
    clients: tuple[IPv4Address, ...]
    listen_port: Annotated[int, Field(ge=0, le=65535)] = DEVELOPER_PORT
    target_port: Annotated[int, Field(ge=1, le=65535)] = 26099
    max_connections: Annotated[int, Field(ge=1)] = 4
    connect_timeout: Annotated[float, Field(gt=0)] = 5.0


async def _pipe(
    source: SocketStream, destination: SocketStream, scope: anyio.CancelScope,
) -> None:
    """Stop both forwarding tasks when either stream ends or fails."""
    try:
        async for chunk in source:
            await destination.send(chunk)
    except (OSError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
        return
    finally:
        scope.cancel()


async def run_bridge(
    config: BridgeConfig,
    *,
    task_status: TaskStatus[int] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Bind one IPv4 interface and relay bounded, allowlisted developer sessions."""
    limiter = anyio.CapacityLimiter(config.max_connections)

    async def handle(client: SocketStream) -> None:
        async with client:
            peer = IPv4Address(client.extra(attribute=SocketAttribute.remote_address)[0])
            if peer not in config.clients:
                logger.warning("bridge.rejected peer=%s", peer)
                return
            if not limiter.available_tokens:
                logger.warning("bridge.capacity peer=%s", peer)
                return
            # No checkpoint between the availability check and acquisition: never queue.
            limiter.acquire_nowait()
            try:
                logger.info("bridge.client peer=%s", peer)
                try:
                    with anyio.fail_after(config.connect_timeout):
                        upstream = await anyio.connect_tcp(
                            str(config.server_host), config.target_port,
                        )
                except (OSError, TimeoutError):
                    logger.warning("bridge.server_unreachable peer=%s", peer)
                    return
                try:
                    async with upstream, anyio.create_task_group() as tasks:
                        tasks.start_soon(_pipe, client, upstream, tasks.cancel_scope)
                        tasks.start_soon(_pipe, upstream, client, tasks.cancel_scope)
                finally:
                    logger.info("bridge.closed peer=%s", peer)
            finally:
                limiter.release()

    async with await anyio.create_tcp_listener(
        local_host=str(config.callback_host), local_port=config.listen_port,
    ) as listener:
        task_status.started(listener.extra(attribute=SocketAttribute.local_port))
        await listener.serve(handle)
