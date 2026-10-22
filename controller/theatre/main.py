"""Manual supervision: no service installation and no startup foreground launch."""

import logging
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from enum import IntEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

import anyio
import typer
from samsung_tv_root.root_agent import RootAgentUnavailableError
from samsung_tv_root.sdb import SdbError

from theatre.archive import LaunchResult, root_lease
from theatre.config import (
    HOME,
    HOME_KEY,
    MOONFIN,
    TARGETS,
    TV_PLUS_KEY,
    AppId,
    ControllerConfig,
    ControllerError,
    Mode,
    Operation,
    RootLostError,
    TelevisionUnavailableError,
)
from theatre.remote import AgentStarter, InputEvent, remote_session

ROUTES: Final = MappingProxyType({HOME_KEY: HOME, TV_PLUS_KEY: MOONFIN})
logger: Final = logging.getLogger("theatre")


class LaunchLease(Protocol):
    """No shell execution capability is exposed to route dispatch."""

    async def installed_apps(self) -> frozenset[AppId]:
        """Return the currently registered permitted app targets."""
        ...

    async def launch(self, app: AppId) -> LaunchResult:
        """Launch exactly one permitted application."""
        ...


class EventSession(Protocol):
    """Authenticated event source, already at READY."""

    async def next_event(self) -> InputEvent:
        """Return the next authenticated, parsed kernel event."""
        ...


class ControllerLease(LaunchLease, AgentStarter, Protocol):
    """Capabilities needed by supervision, without exposing generic root RPC."""

    async def watch_health(self) -> None:
        """Fail if the authenticated root helper is lost."""
        ...


class KeyValue(IntEnum):
    """The three evdev key transitions permitted by the parsed protocol."""

    UP = 0
    DOWN = 1
    REPEAT = 2


async def dispatch(lease: LaunchLease, session: EventSession, mode: Mode) -> None:
    """Launch once per physical down; ignore repeats and paired releases."""
    held: set[int] = set()
    while True:
        event = await session.next_event()
        if mode != Mode.MAP or event.type != 1 or event.code not in ROUTES:
            continue
        match KeyValue(event.value):
            case KeyValue.UP:
                held.discard(event.code)
            case KeyValue.DOWN:
                if event.code not in held:
                    held.add(event.code)
                    target = ROUTES[event.code]
                    result = await lease.launch(target)
                    if not result.success:
                        raise ControllerError(Operation.LAUNCH_FAILED)
                    logger.info("route.launch key=%s target=%s", event.code, target)
            case KeyValue.REPEAT:
                continue


async def control_once(
    config: ControllerConfig,
    acquire: Callable[
        [ControllerConfig], AbstractAsyncContextManager[ControllerLease]
    ] = root_lease,
    connect: Callable[
        [ControllerConfig, ControllerLease], AbstractAsyncContextManager[EventSession]
    ] = remote_session,
) -> None:
    """Validate targets before starting input and preserve playback on every entry."""
    async with acquire(config) as lease:
        if not await lease.installed_apps() >= TARGETS:
            raise ControllerError(Operation.TARGETS_MISSING)
        async with (
            connect(config, lease) as session,
            anyio.create_task_group() as tasks,
        ):
            logger.info("session.ready mode=%s", config.mode.value)
            tasks.start_soon(lease.watch_health)
            try:
                if config.duration is None:
                    await dispatch(lease, session, config.mode)
                else:
                    with anyio.move_on_after(config.duration):
                        await dispatch(lease, session, config.mode)
            finally:
                tasks.cancel_scope.cancel()


async def _wait_for_reconnect(seconds: float) -> None:
    """Wait on a monotonic timer; tests can substitute an exact retry signal."""
    timer = anyio.Event()
    with anyio.move_on_after(seconds):
        await timer.wait()


async def run_controller(config: ControllerConfig) -> None:
    """Reconnect only on explicit opt-in transport loss; launch failures are terminal."""
    while True:
        try:
            await control_once(config)
        except* (
            OSError, anyio.EndOfStream, anyio.BrokenResourceError,
            RootLostError, TelevisionUnavailableError, RootAgentUnavailableError, SdbError,
        ) as failures:
            if not config.reconnect:
                raise
            logger.warning(
                "controller.retry delay_seconds=%s failures=%s",
                config.reconnect_delay,
                ",".join(type(error).__name__ for error in failures.exceptions),
            )
        else:
            return
        await _wait_for_reconnect(config.reconnect_delay)


async def _stop_on_signal(
    signals: AsyncIterator[signal.Signals], scope: anyio.CancelScope
) -> None:
    """Turn Docker/SIGTERM stop into ordered, shielded session teardown."""
    async for received in signals:
        logger.info("controller.stop signal=%s", received.name)
        scope.cancel()
        return


async def serve(config: ControllerConfig) -> None:
    """Subscribe to stop signals before starting any controller activity."""
    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_stop_on_signal, signals, tasks.cancel_scope)
            try:
                await run_controller(config)
            finally:
                tasks.cancel_scope.cancel()


def _run(config: Path) -> None:
    """Run only when explicitly invoked with a private local configuration file."""
    parsed = ControllerConfig.model_validate_json(config.read_bytes())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s level=%(levelname)s event=%(message)s",
    )
    anyio.run(serve, parsed, backend="asyncio")


def main() -> None:
    """Expose a manual CLI, not a network command dispatcher."""
    app = typer.Typer(pretty_exceptions_show_locals=False)
    _ = app.command()(_run)
    app()


if __name__ == "__main__":
    main()
