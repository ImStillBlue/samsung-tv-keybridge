import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import NoReturn

import anyio
import pytest

from tests.routes import MAPPINGS, TARGETS
from tests.test_controller import Events
from tests.test_remote import LoopbackAgent
from theatre import archive, main
from theatre.archive import Application, LaunchResult
from theatre.config import (
    AppId,
    ControllerConfig,
    ControllerError,
    Mode,
    Operation,
    RootLostError,
    TelevisionUnavailableError,
)
from theatre.remote import InputEvent


@dataclass(frozen=True, slots=True)
class ActiveLease:
    agent: LoopbackAgent
    health_started: anyio.Event
    lose_root: anyio.Event
    launches: list[AppId]
    applications: frozenset[AppId] = TARGETS
    launch_success: bool = True

    async def installed_apps(self, requested: frozenset[AppId]) -> frozenset[AppId]:
        return self.applications & requested

    async def list_apps(self) -> tuple[Application, ...]:
        return tuple(Application(app, "Title") for app in self.applications)

    async def launch(self, app: AppId) -> LaunchResult:
        self.launches.append(app)
        return LaunchResult(self.launch_success)

    async def stage_remote(self, port: int, token: bytes, keys: frozenset[int]) -> None:
        await self.agent.stage_remote(port, token, keys)

    async def watch_health(self) -> None:
        self.health_started.set()
        await self.lose_root.wait()
        raise RootLostError


@pytest.mark.anyio
async def test_standby_never_constructs_or_calls_sdk(
    config: ControllerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an explicit off/unreachable power response.
    async def off(_host: IPv4Address) -> bool:
        return False

    def unexpected_sdk(executable: Path, host: str, *, timeout: float) -> NoReturn:
        raise AssertionError((executable, host, timeout))

    monkeypatch.setattr(archive, "television_is_on", off)
    monkeypatch.setattr(archive, "SdbClient", unexpected_sdk)
    # When root is requested.
    with pytest.raises(TelevisionUnavailableError):
        async with archive.root_lease(config):
            pytest.fail("off TV reached a root lease")
    # Then even SDK construction was forbidden by the power gate.


@pytest.mark.anyio
async def test_root_loss_closes_real_input_peer_before_root_lease(
    config: ControllerConfig,
) -> None:
    # Given an authenticated real input callback and a root-health failure signal.
    configured = config.model_copy(update={
        "mode": Mode.MAP, "mappings": MAPPINGS, "duration": None,
    })
    trace: list[str] = []
    closed, started, lost, finished = (anyio.Event() for _ in range(4))
    launches: list[AppId] = []
    async with anyio.create_task_group() as tasks:
        agent = LoopbackAgent(tasks, closed, "map")
        lease = ActiveLease(agent, started, lost, launches)

        @asynccontextmanager
        async def acquire(_config: ControllerConfig) -> AsyncIterator[ActiveLease]:
            trace.append("root.enter")
            try:
                yield lease
            finally:
                assert closed.is_set()
                trace.append("root.close")

        async def run() -> None:
            try:
                with pytest.raises(ExceptionGroup) as failure:
                    await main.control_once(configured, acquire=acquire)
                assert failure.value.subgroup(RootLostError) is not None
            finally:
                finished.set()

        tasks.start_soon(run)
        with anyio.fail_after(2):
            await started.wait()
            # When root disappears while the input socket is otherwise healthy.
            lost.set()
            await finished.wait()
        tasks.cancel_scope.cancel()
    # Then EOF/cleanup precedes root teardown and startup never steals foreground.
    assert trace == ["root.enter", "root.close"]
    assert launches == []


@pytest.mark.anyio
async def test_missing_targets_prevent_input_start(config: ControllerConfig) -> None:
    # Given a root inventory without the required widget.
    trace: list[str] = []

    @asynccontextmanager
    async def acquire(_config: ControllerConfig) -> AsyncIterator[main.ControllerLease]:
        class MissingTargets:
            async def list_apps(self) -> tuple[Application, ...]:
                return ()

            async def installed_apps(self, requested: frozenset[AppId]) -> frozenset[AppId]:
                return requested & frozenset()

            async def launch(self, app: AppId) -> LaunchResult:
                raise AssertionError(app)

            async def stage_remote(self, port: int, token: bytes, keys: frozenset[int]) -> None:
                raise AssertionError((port, len(token), keys))

            async def watch_health(self) -> None:
                raise AssertionError

        try:
            yield MissingTargets()
        finally:
            trace.append("root.close")

    # When target readiness is checked.
    with pytest.raises(ControllerError) as failure:
        await main.control_once(
            config.model_copy(update={"mode": Mode.MAP, "mappings": MAPPINGS}), acquire=acquire,
        )
    # Then no input session starts and the root lease still closes.
    assert failure.value.operation == Operation.TARGETS_MISSING
    assert trace == ["root.close"]


@pytest.mark.anyio
async def test_sigterm_runs_controller_cleanup(
    config: ControllerConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a running service whose stop handler is subscribed before readiness.
    ready, closed = anyio.Event(), anyio.Event()

    async def running(_config: ControllerConfig) -> None:
        ready.set()
        try:
            await anyio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(main, "run_controller", running)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(main.serve, config)
        with anyio.fail_after(2):
            await ready.wait()
            # When Docker's real stop signal reaches this process.
            os.kill(os.getpid(), signal.SIGTERM)
            await closed.wait()
        tasks.cancel_scope.cancel()
    # Then the service unwinds through cleanup, rather than process termination.
    assert closed.is_set()


@pytest.mark.anyio
async def test_reconnect_waits_for_retry_signal_without_launching(
    config: ControllerConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given one lost transport and an explicitly armed reconnect policy.
    configured = config.model_copy(update={
        "mode": Mode.MAP, "mappings": MAPPINGS, "duration": None, "reconnect": True,
    })
    waiting, retry, finished = anyio.Event(), anyio.Event(), anyio.Event()
    attempts: list[ControllerConfig] = []

    async def control(actual: ControllerConfig) -> None:
        attempts.append(actual)
        if len(attempts) == 1:
            raise RootLostError

    async def delay(seconds: float) -> None:
        assert seconds == configured.reconnect_delay
        waiting.set()
        await retry.wait()

    async def run() -> None:
        try:
            await main.run_controller(configured)
        finally:
            finished.set()

    monkeypatch.setattr(main, "control_once", control)
    monkeypatch.setattr(main, "_wait_for_reconnect", delay)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run)
        with anyio.fail_after(2):
            await waiting.wait()
            assert attempts == [configured]
            # When the exact retry deadline signal is delivered.
            retry.set()
            await finished.wait()
        tasks.cancel_scope.cancel()
    # Then reconnection only re-enters supervision; it invents no foreground action.
    assert attempts == [configured, configured]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [Mode.MAP, Mode.OBSERVE])
async def test_inventory_filters_routes_before_input_and_logs_discovery(
    config: ControllerConfig, caplog: pytest.LogCaptureFixture, mode: Mode,
) -> None:
    # Given only one installed target, When starting, Then only its key can be intercepted.
    caplog.set_level("INFO", logger="theatre")
    connected: list[ControllerConfig] = []
    launches: list[AppId] = []
    configured = config.model_copy(update={
        "mode": mode, "mappings": MAPPINGS if mode == Mode.MAP else (),
    })
    async with anyio.create_task_group() as tasks:
        agent = LoopbackAgent(tasks, anyio.Event(), mode.value)
        lease = ActiveLease(
            agent, anyio.Event(), anyio.Event(), launches, frozenset({MAPPINGS[0].app_id}),
        )

        @asynccontextmanager
        async def acquire(_config: ControllerConfig) -> AsyncIterator[ActiveLease]:
            yield lease

        @asynccontextmanager
        async def connect(
            actual: ControllerConfig, _lease: main.ControllerLease,
        ) -> AsyncIterator[Events]:
            connected.append(actual)
            yield Events((
                InputEvent(1, 1, 63, 1, mode != Mode.MAP),
                InputEvent(2, 1, 560, 1, forwarded=True),
            ), [0])

        with pytest.raises(ExceptionGroup) as failure:
            await main.control_once(configured, acquire=acquire, connect=connect)
        assert failure.value.subgroup(anyio.EndOfStream) is not None
        tasks.cancel_scope.cancel()
    if mode == Mode.MAP:
        assert connected[0].mappings == (MAPPINGS[0],)
        assert launches == [MAPPINGS[0].app_id]
        assert (
            "mapping.skipped key=tv_plus app=MoonfinApp.moonfin reason=not-installed"
            in caplog.text
        )
    else:
        assert connected[0].mappings == ()
        assert launches == []
        assert "discover.app id=Theatre001.Home name=Title" in caplog.text
