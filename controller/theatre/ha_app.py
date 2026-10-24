"""Home Assistant options, owned SDB process and opt-in developer transport."""

import logging
import signal
import socket
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, ClassVar, Final, Self

import anyio
from anyio.abc import Process
from anyio.streams.buffered import BufferedByteReceiveStream
from pydantic import BaseModel, ConfigDict, Field, model_validator

from theatre.bridge import BridgeConfig, run_bridge
from theatre.config import (
    BROADCAST_ADDRESS,
    AddressPolicyError,
    ControllerConfig,
    KeyMapping,
    MappingPolicyError,
    Mode,
    RemoteName,
)
from theatre.main import run_controller

SDB_PORT: Final = 26099
logger: Final = logging.getLogger("theatre")


class BridgePolicyError(ValueError):
    """Developer transport requires an explicit trusted client allowlist."""

    def __init__(self) -> None:
        """Explain why a bridge without trusted peers cannot start."""
        super().__init__("developer bridge requires at least one developer client")


class AppOptions(BaseModel):
    """User setup settings; executable paths remain private to the app."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", strict=True, hide_input_in_errors=True,
    )
    enabled: bool = False
    tv_host: str = ""
    callback_host: str = ""
    reconnect_delay: Annotated[int, Field(ge=5, le=60)] = 15
    discover: bool = False
    developer_bridge: bool = False
    developer_clients: tuple[str, ...] = ()
    remote_name: RemoteName | None = None
    mappings: tuple[KeyMapping, ...] = ()

    @model_validator(mode="after")
    def developer_access(self) -> Self:
        """Parse explicit unicast peers and require valid hosts before binding."""
        if self.developer_bridge and not self.developer_clients:
            raise BridgePolicyError
        addresses = tuple(IPv4Address(client) for client in self.developer_clients)
        if self.developer_bridge:
            addresses += (IPv4Address(self.tv_host), IPv4Address(self.callback_host))
        if any(
            address.is_unspecified or address.is_multicast or address == BROADCAST_ADDRESS
            for address in addresses
        ):
            raise AddressPolicyError
        return self

    @model_validator(mode="after")
    def explicit_mappings(self) -> Self:
        """Require explicit unique mappings before enabling interception."""
        if (
            (self.enabled and not self.discover and not self.mappings)
            or len({m.key for m in self.mappings}) != len(self.mappings)
        ):
            raise MappingPolicyError
        return self

    def controller(self) -> ControllerConfig:
        """Convert enabled app settings to the existing unattended policy."""
        return ControllerConfig(
            tv_host=IPv4Address(self.tv_host),
            callback_host=IPv4Address(self.callback_host),
            sdb=Path("/opt/keybridge/sdb"),
            payloads=Path("/opt/keybridge/payloads"),
            state_dir=Path("/data/state"),
            allow_root=self.enabled,
            mode=Mode.OBSERVE if self.discover else Mode.MAP,
            mappings=() if self.discover else self.mappings,
            remote_name=self.remote_name,
            duration=None,
            reconnect=True,
            reconnect_delay=float(self.reconnect_delay),
        )


class SdbServerError(RuntimeError):
    """A failed or conflicting SDB server must not be silently replaced."""

    def __init__(self, detail: str) -> None:
        """Report startup/protocol/process failure without exposing credentials."""
        super().__init__(f"sdb.server_failed: {detail}")


async def check_server(port: int) -> None:
    """Check SDB's wire protocol without a client that might auto-spawn a server."""
    async with await anyio.connect_tcp("127.0.0.1", port) as connection:
        await connection.send(b"000chost:version")
        stream = BufferedByteReceiveStream(connection)
        if await stream.receive_exactly(4) != b"OKAY":
            detail = "version request rejected"
            raise SdbServerError(detail)
        length = int(await stream.receive_exactly(4), 16)
        if length != len(b"4.2.36") or await stream.receive_exactly(length) != b"4.2.36":
            detail = "unexpected version"
            raise SdbServerError(detail)


def log_port_holders(port: int) -> None:
    """Host networking shares /proc/net/tcp; record what occupies the port."""
    suffix = f":{port:04X}"
    for line in Path("/proc/net/tcp").read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if fields[1].endswith(suffix):
            logger.warning(
                "sdb.port_busy local=%s remote=%s state=%s uid=%s inode=%s",
                fields[1], fields[2], fields[3], fields[7], fields[9],
            )


@asynccontextmanager
async def sdb_server(executable: Path, port: int = SDB_PORT) -> AsyncIterator[Process]:
    """Own the foreground server, never touching another app's existing server."""
    with socket.socket() as probe:
        # TIME_WAIT from a just-stopped server must not block a restart; Linux
        # still refuses this bind while any process is listening on the port.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            log_port_holders(port)
            raise
    async with await anyio.open_process(
        [str(executable), "fork-server", "server"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=None,
    ) as process:
        try:
            with anyio.fail_after(15):
                # This pinned SDB closes inherited stdout after server initialization.
                if process.stdout is None:
                    detail = "missing startup pipe"
                    raise SdbServerError(detail)
                async for _chunk in process.stdout:
                    pass
                await check_server(port)
            if process.returncode is not None:
                detail = f"startup exit {process.returncode}"
                raise SdbServerError(detail)
            logger.info("sdb.ready")
            yield process
        finally:
            with anyio.CancelScope(shield=True):
                if process.returncode is None:
                    process.terminate()
                    with anyio.move_on_after(5) as deadline:
                        _ = await process.wait()
                    if deadline.cancel_called:
                        process.kill()
                        _ = await process.wait()


async def watch_server(process: Process) -> None:
    """Loss of the owned server cancels controller sessions before app exit."""
    code = await process.wait()
    detail = f"exit {code}"
    raise SdbServerError(detail)


async def run_enabled(options: AppOptions) -> None:
    """Start opted-in workers after readiness and stop them before the server."""
    Path("/data/home").mkdir(mode=0o700, parents=True, exist_ok=True)
    async with (
        sdb_server(Path("/opt/keybridge/sdb")) as process,
        anyio.create_task_group() as tasks,
    ):
        tasks.start_soon(watch_server, process)
        try:
            if options.developer_bridge:
                tasks.start_soon(
                    run_bridge,
                    BridgeConfig(
                        callback_host=IPv4Address(options.callback_host),
                        clients=tuple(IPv4Address(client) for client in options.developer_clients),
                    ),
                )
            if options.enabled:
                await run_controller(options.controller())
            else:
                await anyio.sleep_forever()
        finally:
            tasks.cancel_scope.cancel()


async def stop_on_signal(
    signals: AsyncIterator[signal.Signals], scope: anyio.CancelScope,
) -> None:
    """Subscribe before starting SDB, including cancellation during startup."""
    async for received in signals:
        logger.info("app.stop signal=%s", received.name)
        scope.cancel()
        return


async def run_app(options: AppOptions) -> None:
    """Supervise opted-in developer transport independently of controller enablement."""
    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(stop_on_signal, signals, tasks.cancel_scope)
            try:
                if options.enabled or options.developer_bridge:
                    await run_enabled(options)
                else:
                    logger.info("app.disabled; enable after configuring TV and callback addresses")
                    await anyio.sleep_forever()
            finally:
                tasks.cancel_scope.cancel()


def main() -> None:
    """Load the Supervisor-owned options without writing secrets or configuration."""
    options = AppOptions.model_validate_json(Path("/data/options.json").read_bytes())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s level=%(levelname)s event=%(message)s",
    )
    # Power/health polling runs every few seconds; keep only HTTP warnings.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    anyio.run(run_app, options)


if __name__ == "__main__":
    main()
