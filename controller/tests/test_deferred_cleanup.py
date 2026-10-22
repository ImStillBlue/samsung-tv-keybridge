from ipaddress import IPv4Address
from pathlib import Path

import pytest
from samsung_tv_root.root_agent import RootAgentServer
from samsung_tv_root.sdb import SdbClient

from theatre import bootstrap
from theatre.config import ControllerConfig
from theatre.state import BootstrapLedger


@pytest.mark.anyio
async def test_power_loss_retains_stage_without_sdk_cleanup(
    config: ControllerConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an owned SDK stage and a TV that powers off during the session.
    configured = config.model_copy(update={"state_dir": tmp_path / "state"})
    powered_on = [True]
    removed: list[str] = []
    staged: list[str] = []

    async def power(_host: IPv4Address) -> bool:
        return powered_on[0]

    async def prepare(_directory: Path, package: str, _server: RootAgentServer) -> None:
        staged.append(package)

    def stage(_client: SdbClient, _directory: Path, _payloads: Path) -> None:
        return

    def cleanup(_client: SdbClient, package: str) -> None:
        removed.append(package)

    monkeypatch.setattr(bootstrap, "television_is_on", power)
    monkeypatch.setattr(bootstrap, "prepare_artifacts", prepare)
    monkeypatch.setattr(bootstrap, "stage", stage)
    monkeypatch.setattr(bootstrap, "cleanup", cleanup)
    server = RootAgentServer("127.0.0.1", 0, "127.0.0.1", bytes(32))
    client = SdbClient(Path("/not-a-real-sdk"), "127.0.0.1")
    # When the stage owner closes with explicit off status.
    async with bootstrap.sdk_bootstrap(configured, client, server):
        powered_on[0] = False
    # Then no active SDK command is attempted and ownership survives for recovery.
    assert removed == []
    with BootstrapLedger(configured.state_dir, configured.tv_host) as ledger:
        assert ledger.packages == tuple(staged)


@pytest.mark.anyio
async def test_reconnect_cleans_known_pending_stage_before_new_stage(
    config: ControllerConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a previous power-loss record, not a guessed scan of SDK directories.
    configured = config.model_copy(update={"state_dir": tmp_path / "state"})
    previous = "archive-root-0123456789abcdef"
    with BootstrapLedger(configured.state_dir, configured.tv_host) as ledger:
        ledger.add(previous)
    trace: list[str] = []

    async def power(_host: IPv4Address) -> bool:
        return True

    async def prepare(_directory: Path, package: str, _server: RootAgentServer) -> None:
        trace.append("prepare:" + package)

    def stage(_client: SdbClient, _directory: Path, _payloads: Path) -> None:
        trace.append("stage")

    def cleanup(_client: SdbClient, package: str) -> None:
        trace.append("cleanup:" + package)

    monkeypatch.setattr(bootstrap, "television_is_on", power)
    monkeypatch.setattr(bootstrap, "prepare_artifacts", prepare)
    monkeypatch.setattr(bootstrap, "stage", stage)
    monkeypatch.setattr(bootstrap, "cleanup", cleanup)
    server = RootAgentServer("127.0.0.1", 0, "127.0.0.1", bytes(32))
    client = SdbClient(Path("/not-a-real-sdk"), "127.0.0.1")
    # When the TV is on again and a new stage is acquired.
    async with bootstrap.sdk_bootstrap(configured, client, server):
        assert trace[0] == "cleanup:" + previous
        assert trace[-1] == "stage"
    # Then every confirmed removal is forgotten, including normal new-session cleanup.
    with BootstrapLedger(configured.state_dir, configured.tv_host) as ledger:
        assert ledger.packages == ()
