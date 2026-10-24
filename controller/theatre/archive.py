"""Archive-root acquisition and a deliberately narrow application-launch lease."""

import re
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath
from typing import ClassVar, Protocol, final

import anyio
from anyio.to_thread import run_sync
from pydantic import BaseModel, ConfigDict, Field
from samsung_tv_root.archive_root import SYSTEM_HASH_COMMAND
from samsung_tv_root.root_agent import (
    RootAgentError,
    RootAgentIdentity,
    RootAgentResult,
    RootAgentServer,
    RootAgentWrittenFile,
    generate_secret,
)
from samsung_tv_root.sdb import SdbClient

from theatre.bootstrap import assess, capture, sdk_bootstrap
from theatre.config import (
    APP_ID_PATTERN,
    HEARTBEAT_SECONDS,
    MAX_APP_ID_LENGTH,
    MAX_PROCESS_ID,
    AppId,
    ControllerConfig,
    ControllerError,
    Mode,
    Operation,
    RootLostError,
    TelevisionUnavailableError,
)
from theatre.power import television_is_on


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """Native launch completion; stderr is deliberately not exposed to dispatch."""

    success: bool


class RootConnection(Protocol):
    """Pinned root connection operations used only inside this module."""

    identity: RootAgentIdentity

    @property
    def closed(self) -> bool:
        """Whether a failed/cancelled RPC has invalidated the root transport."""
        ...

    async def execute(self, command: str, seconds: float, /) -> RootAgentResult:
        """Execute the pinned RPC with a positional protocol deadline."""
        ...

    async def write_file(self, path: str, data: bytes) -> RootAgentWrittenFile:
        """Write a root-owned private file inside the runtime directory."""
        ...

    async def ping(self, seconds: float, /) -> RootAgentIdentity:
        """Require an authenticated live root identity."""
        ...

    async def shutdown(self, seconds: float, /) -> None:
        """Request normal root-helper exit."""
        ...

    async def close(self) -> None:
        """Close the accepted root connection."""
        ...


class AgentConfiguration(BaseModel):
    """Exact private managed-agent configuration, sent only over the root channel."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True
    )
    host: str
    port: int
    token: str = Field(repr=False)
    mode: Mode
    keys: tuple[int, ...]
    remote_name: str | None


@dataclass(frozen=True, slots=True)
class Application:
    """One exact native inventory record."""

    id: AppId
    name: str


@final
class ArchiveLease:
    """Mutable ownership of a root connection and this session's remote process.

    The dispatcher receives no general shell API. Staging uses the authenticated
    root agent's 0700 runtime directory, never the SDK-writable bootstrap directory.
    """

    def __init__(self, connection: RootConnection, config: ControllerConfig) -> None:
        """Own one validated root transport and its private input-agent stage."""
        self._connection = connection
        self._config = config
        self._directory = PurePosixPath(f"/run/samsung-tv-root/agent-{connection.identity.pid}")
        self._remote_pid: int | None = None
        self._closed = False

    async def list_apps(self) -> tuple[Application, ...]:
        """Read exact title/ID lines, never substrings in diagnostic output."""
        result = await self._connection.execute(
            "/usr/bin/app_launcher -l", self._config.command_timeout
        )
        if result.exit_code != 0 or result.timed_out:
            raise ControllerError(Operation.APP_INVENTORY)
        applications: list[Application] = []
        for line in result.stdout.splitlines():
            record = re.fullmatch(r"\s*'([^'\r\n]*)'\s+'([^'\r\n]*)'\s*", line)
            if (
                record and re.fullmatch(APP_ID_PATTERN, record[2])
                and len(record[2]) <= MAX_APP_ID_LENGTH
            ):
                applications.append(Application(AppId(record[2]), record[1]))
        return tuple(applications)

    async def installed_apps(self, requested: frozenset[AppId]) -> frozenset[AppId]:
        """Return only requested IDs registered as exact inventory records."""
        return frozenset(app.id for app in await self.list_apps() if app.id in requested)

    async def launch(self, app: AppId) -> LaunchResult:
        """Launch one permitted, registered application without a shell extension."""
        if app not in {mapping.app_id for mapping in self._config.mappings}:
            raise ControllerError(Operation.APP_FORBIDDEN)
        if not await television_is_on(self._config.tv_host):
            raise TelevisionUnavailableError
        try:
            result = await self._connection.execute(
                f"/usr/bin/app_launcher -s {shlex.quote(app)}", self._config.command_timeout
            )
        except (OSError, TimeoutError) as error:
            raise ControllerError(Operation.LAUNCH_TRANSPORT) from error
        return LaunchResult(result.exit_code == 0 and not result.timed_out)

    async def stage_remote(self, port: int, token: bytes, keys: frozenset[int]) -> None:
        """Stage and start the input agent with only a private config path in argv."""
        directory = self._directory
        for name in ("TheatreInputAgent.dll", "TheatreInputAgent.runtimeconfig.json"):
            _ = await self._connection.write_file(
                str(directory / name), (self._config.payloads / name).read_bytes()
            )
        private = AgentConfiguration(
            host=str(self._config.callback_host), port=port,
            token=token.hex(), mode=self._config.mode,
            keys=tuple(sorted(keys)), remote_name=self._config.remote_name,
        )
        # WRITEFILE creates root-owned files with mode 0600 in the root-owned
        # 0700 runtime directory. No token is included in an EXEC command or log.
        _ = await self._connection.write_file(
            str(directory / "input.json"), private.model_dump_json().encode()
        )
        command = (
            f"/usr/bin/dotnet {directory}/TheatreInputAgent.dll {directory}/input.json "
            f">{directory}/input.log 2>&1 </dev/null & echo $!"
        )
        result = await self._connection.execute(command, self._config.command_timeout)
        if result.exit_code != 0 or result.timed_out or not result.stdout.strip().isdecimal():
            raise ControllerError(Operation.REMOTE_START)
        process_id = int(result.stdout.strip())
        if not 0 < process_id <= MAX_PROCESS_ID:
            raise ControllerError(Operation.REMOTE_START)
        self._remote_pid = process_id

    async def watch_health(self) -> None:
        """End input ownership if the root helper disappears or loses root."""
        timer = anyio.Event()
        while True:
            if not await television_is_on(self._config.tv_host):
                raise TelevisionUnavailableError
            try:
                identity = await self._connection.ping(self._config.command_timeout)
            except (RootAgentError, OSError, TimeoutError) as error:
                raise RootLostError from error
            if any(value != 0 for value in (
                identity.uid, identity.euid, identity.gid, identity.egid
            )):
                raise ControllerError(Operation.ROOT_IDENTITY)
            with anyio.move_on_after(HEARTBEAT_SECONDS):
                await timer.wait()

    async def close(self) -> None:
        """Stop only our agent, then shut down the root lease, even after failure."""
        if self._closed:
            return
        self._closed = True
        powered_on = False
        try:
            powered_on = (
                not self._connection.closed
                and await television_is_on(self._config.tv_host)
            )
            if self._remote_pid is not None and not self._connection.closed and powered_on:
                # Close of the accepted input socket has already requested release.
                # SIGTERM additionally bounds an unresponsive owned child.
                command = " ".join([
                    f"if test -r /proc/{self._remote_pid}/cmdline &&",
                    f"tr '\\000' '\\n' </proc/{self._remote_pid}/cmdline |",
                    f"grep -Fxq {self._directory}/input.json; then",
                    f"kill -TERM {self._remote_pid}; fi",
                ])
                _ = await self._connection.execute(
                    command,
                    self._config.command_timeout,
                )
        finally:
            try:
                if not self._connection.closed and powered_on:
                    await self._connection.shutdown(self._config.command_timeout)
            finally:
                await self._connection.close()


@asynccontextmanager
async def root_lease(config: ControllerConfig) -> AsyncIterator[ArchiveLease]:
    """Own one per-run SDK bootstrap and one authenticated root connection."""
    if not config.allow_root:
        raise ControllerError(Operation.ROOT_OPT_IN)
    if not await television_is_on(config.tv_host):
        raise TelevisionUnavailableError
    for name in (
        "SamsungTvArchiveRoot.dll", "SamsungTvRootAgent.dll",
        "TheatreInputAgent.dll", "TheatreInputAgent.runtimeconfig.json",
    ):
        if not (config.payloads / name).is_file():
            raise ControllerError(Operation.PAYLOAD_MISSING)
    client = SdbClient(config.sdb, str(config.tv_host), timeout=config.command_timeout)
    assessment = await run_sync(assess, client)
    server = RootAgentServer(
        str(config.callback_host), 0, str(config.tv_host), generate_secret(), require_root=True
    )
    lease: ArchiveLease | None = None
    await server.start()
    try:
        async with (
            sdk_bootstrap(config, client, server) as command,
            anyio.create_task_group() as tasks,
        ):
            # Do not wrap the detached bootstrap in an SDK log callback. Its
            # authenticated root callback, not appinstall's status, proves startup.
            tasks.start_soon(
                partial(run_sync,
                        partial(client.inject, command, timeout=90),
                        abandon_on_cancel=True)
            )
            try:
                connection = await server.accept(config.accept_timeout)
                lease = ArchiveLease(connection, config)
                identity = await connection.ping(config.command_timeout)
                if any(value != 0 for value in (
                    identity.uid, identity.euid, identity.gid, identity.egid
                )):
                    raise ControllerError(Operation.ROOT_IDENTITY)
                yield lease
            finally:
                with anyio.CancelScope(shield=True):
                    if lease is not None:
                        await lease.close()
                tasks.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            try:
                if lease is not None:
                    await lease.close()
            finally:
                try:
                    await server.close()
                finally:
                    if await television_is_on(config.tv_host) and (
                        await run_sync(
                            capture, client, SYSTEM_HASH_COMMAND
                        )
                    ) != assessment.original_hashes:
                        raise ControllerError(Operation.SYSTEM_HASH)
