import hashlib
from ipaddress import IPv4Address
from pathlib import Path
from typing import final

import pytest
from samsung_tv_root.root_agent import (
    RootAgentIdentity,
    RootAgentResult,
    RootAgentWrittenFile,
)

from theatre import archive
from theatre.archive import ArchiveLease
from theatre.config import (
    HOME,
    MOONFIN,
    TARGETS,
    AppId,
    ControllerConfig,
    ControllerError,
    Operation,
    RootLostError,
    TelevisionUnavailableError,
)


@pytest.fixture(autouse=True)
def power_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def on(_host: IPv4Address) -> bool:
        return True

    monkeypatch.setattr(archive, "television_is_on", on)


@final
class Connection:
    """Stateful root RPC fixture; no SDK or native TV action is executed."""

    def __init__(self) -> None:
        self.identity = RootAgentIdentity(321, 0, 0, 0, 0, "0", "User")
        self.is_closed = False
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {}
        self.lifecycle: list[str] = []
        self.output = ""
        self.exit_code = 0
        self.ping_failure = False

    @property
    def closed(self) -> bool:
        return self.is_closed

    async def execute(self, command: str, seconds: float, /) -> RootAgentResult:
        assert seconds > 0
        self.commands.append(command)
        return RootAgentResult(
            len(self.commands), self.exit_code, timed_out=False, stdout=self.output, stderr=""
        )

    async def write_file(self, path: str, data: bytes) -> RootAgentWrittenFile:
        self.files[path] = data
        return RootAgentWrittenFile(1, len(data), hashlib.sha256(data).hexdigest())

    async def ping(self, seconds: float, /) -> RootAgentIdentity:
        assert seconds > 0
        self.lifecycle.append("ping")
        if self.ping_failure:
            raise BrokenPipeError
        return self.identity

    async def shutdown(self, seconds: float, /) -> None:
        assert seconds > 0
        self.lifecycle.append("shutdown")
        self.is_closed = True

    async def close(self) -> None:
        self.lifecycle.append("close")
        self.is_closed = True


@pytest.mark.anyio
async def test_registry_matches_whole_permitted_ids(config: ControllerConfig) -> None:
    # Given lookalike registry identifiers and the two real targets.
    connection = Connection()
    connection.output = f"{HOME}.evil\nprefix{MOONFIN}\n{HOME}\n{MOONFIN}\n"
    lease = ArchiveLease(connection, config)
    # When the native registry is parsed.
    actual = await lease.installed_apps()
    # Then only the exact required targets count toward readiness.
    assert actual == TARGETS


@pytest.mark.anyio
async def test_registry_lookalikes_do_not_satisfy_readiness(config: ControllerConfig) -> None:
    # Given only similarly named packages.
    connection = Connection()
    connection.output = f"{HOME}.evil\nprefix{MOONFIN}\n"
    # When querying the restricted inventory.
    actual = await ArchiveLease(connection, config).installed_apps()
    # Then no target was invented.
    assert actual == frozenset()


@pytest.mark.anyio
async def test_dispatch_capability_rejects_arbitrary_apps(config: ControllerConfig) -> None:
    # Given a lease with no generic dispatcher shell interface.
    connection = Connection()
    # When an unpermitted application is requested.
    with pytest.raises(ControllerError) as failure:
        _ = await ArchiveLease(connection, config).launch(AppId("Other.App"))
    # Then rejection occurs before any root command.
    assert failure.value.operation == Operation.APP_FORBIDDEN
    assert connection.commands == []


@pytest.mark.anyio
@pytest.mark.parametrize("pid", ["0", "-1", "2147483648", "wrong", "", "1\n2"])
async def test_invalid_remote_pid_cannot_become_a_kill_target(
    config: ControllerConfig, tmp_path: Path, pid: str,
) -> None:
    # Given a valid private payload stage but an invalid native process result.
    for name in ("TheatreInputAgent.dll", "TheatreInputAgent.runtimeconfig.json"):
        _ = (tmp_path / name).write_bytes(b"fixture")
    configured = config.model_copy(update={"payloads": tmp_path})
    connection = Connection()
    connection.output = pid
    lease = ArchiveLease(connection, configured)
    # When the untrusted launch result is parsed.
    with pytest.raises(ControllerError) as failure:
        await lease.stage_remote(12345, bytes(32))
    await lease.close()
    # Then no dangerous/unknown PID reaches native cleanup.
    assert failure.value.operation == Operation.REMOTE_START
    assert not any("kill -TERM" in command for command in connection.commands)


@pytest.mark.anyio
async def test_input_secret_is_only_in_private_writefile_payload(
    config: ControllerConfig, tmp_path: Path,
) -> None:
    # Given a fresh secret and staged input-agent assembly.
    for name in ("TheatreInputAgent.dll", "TheatreInputAgent.runtimeconfig.json"):
        _ = (tmp_path / name).write_bytes(b"fixture")
    connection = Connection()
    connection.output = "456\n"
    token = bytes(range(32))
    lease = ArchiveLease(connection, config.model_copy(update={"payloads": tmp_path}))
    # When the input process is staged.
    await lease.stage_remote(12345, token)
    # Then its exact config is sent through authenticated WRITEFILE, not argv/logs.
    private = archive.AgentConfiguration.model_validate_json(
        connection.files["/run/samsung-tv-root/agent-321/input.json"]
    )
    assert private.host == "127.0.0.1"
    assert private.port == 12345
    assert private.mode.value == "observe"
    assert private.token == token.hex()
    assert all(token.hex() not in command for command in connection.commands)


@pytest.mark.anyio
async def test_closed_root_transport_does_not_skip_local_close(config: ControllerConfig) -> None:
    # Given an already lost root connection.
    connection = Connection()
    connection.is_closed = True
    # When its lease is closed.
    await ArchiveLease(connection, config).close()
    # Then no invalid RPC is attempted and local closure still occurs.
    assert connection.commands == []
    assert connection.lifecycle == ["close"]


@pytest.mark.anyio
async def test_power_off_ends_health_without_root_ping(
    config: ControllerConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an off response during an established lease.
    async def off(_host: IPv4Address) -> bool:
        return False

    monkeypatch.setattr(archive, "television_is_on", off)
    connection = Connection()
    # When root health is supervised.
    with pytest.raises(TelevisionUnavailableError):
        await ArchiveLease(connection, config).watch_health()
    # Then the off TV receives no additional root RPC.
    assert connection.lifecycle == []


@pytest.mark.anyio
async def test_root_ping_failure_is_recoverable(
    config: ControllerConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an on TV whose authenticated root transport fails.
    async def on(_host: IPv4Address) -> bool:
        return True

    monkeypatch.setattr(archive, "television_is_on", on)
    connection = Connection()
    connection.ping_failure = True
    # When the root heartbeat runs.
    with pytest.raises(RootLostError):
        await ArchiveLease(connection, config).watch_health()
    # Then input supervision receives an explicit transport-loss signal.
    assert connection.lifecycle == ["ping"]
