from dataclasses import dataclass

import anyio
import pytest

from tests.routes import HOME, MOONFIN, ROUTES, TARGETS
from theatre.archive import Application, LaunchResult
from theatre.config import AppId, ControllerError, Mode
from theatre.main import dispatch
from theatre.remote import InputEvent


@dataclass(frozen=True, slots=True)
class LaunchRecorder:
    launches: list[AppId]
    success: bool = True

    async def installed_apps(self, requested: frozenset[AppId]) -> frozenset[AppId]:
        return TARGETS & requested

    async def list_apps(self) -> tuple[Application, ...]:
        return ()

    async def launch(self, app: AppId) -> LaunchResult:
        self.launches.append(app)
        return LaunchResult(self.success)


@dataclass(frozen=True, slots=True)
class Events:
    values: tuple[InputEvent, ...]
    remaining: list[int]

    async def next_event(self) -> InputEvent:
        index = self.remaining[0]
        if index == len(self.values):
            raise anyio.EndOfStream
        self.remaining[0] += 1
        return self.values[index]


@pytest.mark.anyio
async def test_routes_launch_once_per_down_and_preserve_other_keys() -> None:
    # Given physical down/repeat/duplicate-down/release traffic for both routes.
    events = Events(tuple(
        InputEvent(index + 1, 1, code, value, code not in {63, 560})
        for index, (code, value) in enumerate((
            (63, 1), (63, 2), (63, 1), (63, 0), (560, 1), (560, 0),
            (42, 1), (42, 0), (63, 1), (63, 0),
        ))
    ), [0])
    lease = LaunchRecorder([])
    # When dispatch processes those events to EOF.
    with pytest.raises(anyio.EndOfStream):
        await dispatch(lease, events, Mode.MAP, ROUTES)
    # Then only the fixed targets launch, once for each physical down.
    assert lease.launches == [HOME, MOONFIN, HOME]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [Mode.OBSERVE, Mode.FORWARD])
async def test_manual_nonmapping_modes_never_launch(mode: Mode) -> None:
    # Given Home in a mode which must preserve native input.
    lease = LaunchRecorder([])
    events = Events((InputEvent(1, 1, 63, 1, mode == Mode.FORWARD),), [0])
    # When the event is dispatched.
    with pytest.raises(anyio.EndOfStream):
        await dispatch(lease, events, mode, {})
    # Then no launch occurs.
    assert lease.launches == []


@pytest.mark.anyio
async def test_native_launch_failure_stops_consuming_keys() -> None:
    # Given a failed native launch and another queued key.
    lease = LaunchRecorder([], success=False)
    events = Events((
        InputEvent(1, 1, 63, 1, forwarded=False),
        InputEvent(2, 1, 560, 1, forwarded=False),
    ), [0])
    # When dispatch receives the first down.
    with pytest.raises(ControllerError):
        await dispatch(lease, events, Mode.MAP, ROUTES)
    # Then the error escapes to ordered session teardown without swallowing more.
    assert lease.launches == [HOME]
    assert events.remaining == [1]


@pytest.mark.anyio
async def test_discovery_logs_safe_and_unknown_key_downs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given safe and unknown downs plus a repeat, When observed, Then only downs are logged.
    caplog.set_level("INFO", logger="theatre")
    events = Events((
        InputEvent(1, 1, 63, 1, forwarded=False),
        InputEvent(2, 1, 42, 1, forwarded=False),
        InputEvent(3, 1, 42, 2, forwarded=False),
    ), [0])
    lease = LaunchRecorder([])
    with pytest.raises(anyio.EndOfStream):
        await dispatch(lease, events, Mode.OBSERVE, {})
    assert "discover.key code=63 name=home mappable=true" in caplog.text
    assert "discover.key code=42 name=unknown mappable=false" in caplog.text
    assert len(caplog.records) == 2
    assert lease.launches == []
