import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import AddressValueError, IPv4Address
from pathlib import Path
from signal import Signals

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream
from pydantic import ValidationError

from tests.routes import MAPPINGS
from theatre import ha_app
from theatre.bridge import BridgeConfig, run_bridge
from theatre.config import ControllerConfig
from theatre.ha_app import AppOptions, SdbServerError, check_server, run_app, sdb_server


@pytest.fixture
def app_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    events: list[str] = []

    def path(value: str) -> Path:
        return tmp_path / "home" if value == "/data/home" else Path(value)

    @asynccontextmanager
    async def server(executable: Path) -> AsyncIterator[None]:
        assert executable == Path("/opt/keybridge/sdb")
        events.append("server.ready")
        try:
            yield
        finally:
            events.append("server.stopped")

    async def watch(process: None) -> None:
        del process
        await anyio.sleep_forever()

    monkeypatch.setattr(ha_app, "Path", path)
    monkeypatch.setattr(ha_app, "sdb_server", server)
    monkeypatch.setattr(ha_app, "watch_server", watch)
    return events


def test_enabled_options_use_fixed_unattended_paths_and_policy() -> None:
    options = AppOptions(
        enabled=True, mappings=MAPPINGS, tv_host="192.0.2.1", callback_host="192.0.2.2",
    )
    config = options.controller()
    assert config.tv_host == IPv4Address("192.0.2.1")
    assert config.callback_host == IPv4Address("192.0.2.2")
    assert config.allow_root
    assert config.reconnect
    assert config.mode.value == "map"
    assert config.duration is None
    assert config.sdb == Path("/opt/keybridge/sdb")
    assert config.state_dir == Path("/data/state")


def test_default_options_cannot_authorize_root() -> None:
    assert not AppOptions.model_validate_json("{}").enabled
    with pytest.raises(AddressValueError):
        _ = AppOptions(enabled=True, discover=True).controller()


def test_discovery_options_are_continuous_without_mappings() -> None:
    # Given explicit discovery, When converted, Then input remains in observe mode.
    options = AppOptions.model_validate_json(json.dumps({
        "enabled": True, "discover": True, "tv_host": "192.0.2.1",
        "callback_host": "192.0.2.2", "remote_name": "Different Samsung",
    }))
    config = options.controller()
    assert config.mode.value == "observe"
    assert config.duration is None
    assert config.reconnect
    assert config.mappings == ()
    assert config.remote_name == "Different Samsung"


def test_json_mappings_convert_without_numeric_key_settings() -> None:
    # Given HA JSON, When parsed, Then explicit safe mappings reach the controller.
    options = AppOptions.model_validate_json(json.dumps({
        "enabled": True, "tv_host": "192.0.2.1", "callback_host": "192.0.2.2",
        "mappings": [{"key": "home", "app_id": "Theatre001.Home"}],
    }))
    assert options.controller().mappings == (MAPPINGS[0],)
    with pytest.raises(ValidationError, match="map mode requires mappings"):
        _ = AppOptions(enabled=True)


@pytest.mark.parametrize("raw", [
    '{"enabled":"true"}', '{"reconnect_delay":0}',
    '{"enabled":true,"command":"arbitrary"}', '{"sdb":"/bin/sh"}',
])
def test_options_reject_invalid_types_and_executable_overrides(raw: str) -> None:
    with pytest.raises(ValidationError):
        _ = AppOptions.model_validate_json(raw)


@pytest.mark.anyio
@pytest.mark.parametrize("reply", [b"OKAY00064.2.36", b"FAIL", b"OKAY00064.0.00"])
async def test_readiness_checks_real_wire_protocol(reply: bytes) -> None:
    received: list[bytes] = []

    async def handle(stream: SocketStream) -> None:
        async with stream:
            received.append(await stream.receive(64))
            await stream.send(reply)

    async with (
        await anyio.create_tcp_listener(local_host="127.0.0.1") as listener,
        anyio.create_task_group() as tasks,
    ):
        tasks.start_soon(listener.serve, handle)
        port = listener.extra(attribute=SocketAttribute.local_port)
        with anyio.fail_after(2):
            if reply == b"OKAY00064.2.36":
                await check_server(port)
            else:
                with pytest.raises(SdbServerError):
                    await check_server(port)
        tasks.cancel_scope.cancel()
    assert received == [b"000chost:version"]


@pytest.mark.anyio
async def test_occupied_sdb_port_never_starts_or_kills_another_server() -> None:
    async with await anyio.create_tcp_listener(local_host="127.0.0.1") as listener:
        port = listener.extra(attribute=SocketAttribute.local_port)
        with pytest.raises(OSError, match=r"\[Errno 98\]"):
            async with sdb_server(Path("/must-not-execute"), port):
                pytest.fail("occupied server was adopted")
        assert listener.extra(attribute=SocketAttribute.local_port) == port


@pytest.mark.anyio
async def test_recently_closed_server_port_does_not_block_restart() -> None:
    # Given a stopped server whose side closed first, leaving TIME_WAIT on its port.
    async def close_first(stream: SocketStream) -> None:
        await stream.aclose()

    port = 0
    async with (
        await anyio.create_tcp_listener(local_host="127.0.0.1") as listener,
        anyio.create_task_group() as tasks,
    ):
        port = listener.extra(attribute=SocketAttribute.local_port)
        tasks.start_soon(listener.serve, close_first)
        async with await anyio.connect_tcp("127.0.0.1", port) as client:
            with pytest.raises(anyio.EndOfStream):
                _ = await client.receive()
        tasks.cancel_scope.cancel()
    # When the owned server starts again, Then the port check passes and launch is attempted.
    with pytest.raises(FileNotFoundError):
        async with sdb_server(Path("/must-not-execute"), port):
            pytest.fail("missing executable started")


def test_developer_access_defaults_off_without_clients() -> None:
    # Given empty HA options, When parsed, Then no developer listener is authorized.
    options = AppOptions.model_validate_json("{}")
    assert not options.developer_bridge
    assert options.developer_clients == ()


@pytest.mark.parametrize("raw", [
    '{"developer_bridge":true}',
    '{"developer_clients":["not-an-ip"]}',
    '{"developer_clients":["::1"]}',
    '{"developer_clients":["0.0.0.0"]}',
    '{"developer_clients":["224.0.0.1"]}',
    '{"developer_clients":["255.255.255.255"]}',
    '{"developer_clients":[123]}',
    '{"developer_bridge":"true"}',
    '{"developer_bridge":true,"developer_clients":["192.0.2.3"]}',
])
def test_developer_access_rejects_missing_clients_bad_ips_and_types(raw: str) -> None:
    # Given unsafe/incomplete HA input, When parsed, Then startup validation fails.
    with pytest.raises(ValidationError):
        _ = AppOptions.model_validate_json(raw)


@pytest.mark.parametrize(
    "host", ["", "::1", str(IPv4Address(0)), "224.0.0.1", "255.255.255.255"],
)
@pytest.mark.parametrize("field", ["tv_host", "callback_host"])
def test_developer_bridge_requires_both_unicast_hosts(host: str, field: str) -> None:
    # Given a disabled controller and invalid bridge host, When parsed, Then reject it.
    options = {
        "developer_bridge": True, "developer_clients": ["127.0.0.1"],
        "tv_host": "127.0.0.1", "callback_host": "127.0.0.1",
    }
    options[field] = host
    with pytest.raises(ValidationError):
        _ = AppOptions.model_validate_json(json.dumps(options))


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("developer_bridge", [False, True])
async def test_app_supervises_bridge_independently_and_cancels_all_workers(
    monkeypatch: pytest.MonkeyPatch, app_server: list[str],
    *, enabled: bool, developer_bridge: bool,
) -> None:
    # Given independently configured workers and a subscribed stop signal.
    bridge_started = anyio.Event()
    controller_started = anyio.Event()
    bridge_stopped = anyio.Event()
    controller_stopped = anyio.Event()
    blocked = anyio.Event()
    peers: list[tuple[IPv4Address, ...]] = []

    async def bridge(config: BridgeConfig) -> None:
        assert app_server == ["server.ready"]
        assert config.server_host == IPv4Address("127.0.0.1")
        assert config.target_port == 26099
        assert config.listen_port == 26098
        assert config.callback_host == IPv4Address("192.0.2.2")
        peers.append(config.clients)
        bridge_started.set()
        try:
            await blocked.wait()
        finally:
            bridge_stopped.set()
            app_server.append("bridge.stopped")

    async def controller(config: ControllerConfig) -> None:
        assert config.allow_root
        controller_started.set()
        try:
            await blocked.wait()
        finally:
            controller_stopped.set()

    async def stop(
        signals: AsyncIterator[Signals], scope: anyio.CancelScope,
    ) -> None:
        del signals
        if developer_bridge:
            await bridge_started.wait()
        if enabled:
            await controller_started.wait()
        scope.cancel()

    monkeypatch.setattr(ha_app, "run_bridge", bridge)
    monkeypatch.setattr(ha_app, "run_controller", controller)
    monkeypatch.setattr(ha_app, "stop_on_signal", stop)
    options = AppOptions(
        enabled=enabled, discover=True, developer_bridge=developer_bridge,
        developer_clients=("192.0.2.3",), tv_host="192.0.2.1", callback_host="192.0.2.2",
    )
    # When the app runs until the subscribed cancellation signal.
    with anyio.fail_after(2):
        await run_app(options)
    # Then only opted-in workers ran and all were stopped before return.
    assert bridge_started.is_set() == developer_bridge
    assert bridge_stopped.is_set() == developer_bridge
    assert controller_started.is_set() == enabled
    assert controller_stopped.is_set() == enabled
    assert peers == ([(IPv4Address("192.0.2.3"),)] if developer_bridge else [])
    assert app_server == (
        ["server.ready", *(["bridge.stopped"] if developer_bridge else []), "server.stopped"]
        if enabled or developer_bridge else []
    )


@pytest.mark.anyio
async def test_app_propagates_developer_bind_failure(
    monkeypatch: pytest.MonkeyPatch, app_server: list[str],
) -> None:
    # Given a disabled controller and an occupied developer listener.
    async with await anyio.create_tcp_listener(local_host="127.0.0.1") as listener:
        port = listener.extra(attribute=SocketAttribute.local_port)

        async def bridge(config: BridgeConfig) -> None:
            await run_bridge(config.model_copy(update={"listen_port": port}))

        monkeypatch.setattr(ha_app, "run_bridge", bridge)
        options = AppOptions(
            developer_bridge=True, developer_clients=("127.0.0.1",),
            tv_host="127.0.0.1", callback_host="127.0.0.1",
        )
        # When the app starts, Then binding fails visibly instead of staying idle.
        with (
            anyio.fail_after(2),
            pytest.RaisesGroup(pytest.RaisesGroup(OSError)),
        ):
            await run_app(options)
    assert app_server == ["server.ready", "server.stopped"]


@pytest.mark.anyio
async def test_bridge_waits_for_server_readiness(
    monkeypatch: pytest.MonkeyPatch, app_server: list[str],
) -> None:
    # Given a server paused before readiness and a disabled controller.
    entering = anyio.Event()
    ready = anyio.Event()
    bridge_started = anyio.Event()

    @asynccontextmanager
    async def server(executable: Path) -> AsyncIterator[None]:
        del executable
        entering.set()
        await ready.wait()
        app_server.append("server.ready")
        yield

    async def bridge(config: BridgeConfig) -> None:
        del config
        assert app_server == ["server.ready"]
        bridge_started.set()
        await anyio.sleep_forever()

    async def stop(
        signals: AsyncIterator[Signals], scope: anyio.CancelScope,
    ) -> None:
        del signals
        await entering.wait()
        assert not bridge_started.is_set()
        ready.set()
        await bridge_started.wait()
        scope.cancel()

    monkeypatch.setattr(ha_app, "sdb_server", server)
    monkeypatch.setattr(ha_app, "run_bridge", bridge)
    monkeypatch.setattr(ha_app, "stop_on_signal", stop)
    options = AppOptions(
        developer_bridge=True, developer_clients=("127.0.0.1",),
        tv_host="127.0.0.1", callback_host="127.0.0.1",
    )
    # When startup is released, Then the bridge starts only after readiness.
    with anyio.fail_after(2):
        await run_app(options)
    assert bridge_started.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
async def test_server_exit_fails_app_and_stops_workers(
    monkeypatch: pytest.MonkeyPatch, app_server: list[str], *, enabled: bool,
) -> None:
    # Given a ready server with a live bridge and optional controller.
    bridge_started = anyio.Event()
    controller_started = anyio.Event()

    async def bridge(config: BridgeConfig) -> None:
        del config
        bridge_started.set()
        try:
            await anyio.sleep_forever()
        finally:
            app_server.append("bridge.stopped")

    async def controller(config: ControllerConfig) -> None:
        del config
        controller_started.set()
        await anyio.sleep_forever()

    async def watch(process: None) -> None:
        del process
        await bridge_started.wait()
        if enabled:
            await controller_started.wait()
        detail = "exit 7"
        raise SdbServerError(detail)

    monkeypatch.setattr(ha_app, "run_bridge", bridge)
    monkeypatch.setattr(ha_app, "run_controller", controller)
    monkeypatch.setattr(ha_app, "watch_server", watch)
    options = AppOptions(
        enabled=enabled, discover=True, developer_bridge=True,
        developer_clients=("127.0.0.1",), tv_host="127.0.0.1", callback_host="127.0.0.1",
    )
    # When the owned server exits, Then the failure propagates after worker cleanup.
    with (
        anyio.fail_after(2),
        pytest.RaisesGroup(pytest.RaisesGroup(pytest.RaisesExc(SdbServerError, match="exit 7"))),
    ):
        await run_app(options)
    assert app_server == ["server.ready", "bridge.stopped", "server.stopped"]
