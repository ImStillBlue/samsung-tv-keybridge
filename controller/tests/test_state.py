import json
import stat
from ipaddress import IPv4Address
from pathlib import Path
from typing import Final

import pytest

from theatre.config import ControllerError, Operation
from theatre.state import BootstrapLedger

HOST: Final = IPv4Address("127.0.0.1")
PACKAGE: Final = "archive-root-0123456789abcdef"


def test_pending_namespace_survives_controller_restart(tmp_path: Path) -> None:
    # Given ownership recorded before SDK staging can partially fail.
    directory = tmp_path / "private"
    with BootstrapLedger(directory, HOST) as ledger:
        ledger.add(PACKAGE)
    # When a new controller instance opens its private ledger.
    with BootstrapLedger(directory, HOST) as restarted:
        # Then the exact deletion target survives process/session teardown.
        assert restarted.packages == (PACKAGE,)
    assert stat.S_IMODE((directory / f"bootstrap-{HOST}.json").stat().st_mode) == 0o600


def test_confirmed_cleanup_forgets_only_its_namespace(tmp_path: Path) -> None:
    # Given two owned, independently generated namespace records.
    other = "archive-root-fedcba9876543210"
    with BootstrapLedger(tmp_path / "private", HOST) as ledger:
        ledger.add(PACKAGE)
        ledger.add(other)
        # When one SDK deletion succeeds.
        ledger.forget(PACKAGE)
        # Then another pending namespace is retained.
        assert ledger.packages == (other,)


def test_a_second_controller_cannot_own_the_same_tv(tmp_path: Path) -> None:
    # Given an active owner of the TV's local recovery ledger.
    directory = tmp_path / "private"
    with BootstrapLedger(directory, HOST):
        # When another controller tries to acquire the same lease.
        with pytest.raises(ControllerError) as failure, BootstrapLedger(directory, HOST):
            pytest.fail("competing controller acquired the lock")
        # Then no simultaneous controller can overwrite ownership records.
        assert failure.value.operation == Operation.CONTROLLER_RUNNING


@pytest.mark.parametrize("packages", [
    ["/etc/shadow"], ["archive-root-../../etc"], ["archive-root-" + "z" * 16],
])
def test_ledger_rejects_non_namespace_deletion_targets(
    tmp_path: Path, packages: list[str],
) -> None:
    # Given a malformed local ledger, never trusted as shell/file paths.
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    _ = (directory / f"bootstrap-{HOST}.json").write_text(
        json.dumps({"packages": packages}), encoding="utf-8"
    )
    # When records cross the filesystem boundary.
    with pytest.raises(ControllerError) as failure, BootstrapLedger(directory, HOST):
        pytest.fail("unsafe namespace accepted")
    # Then no SDK action can consume them.
    assert failure.value.operation == Operation.STATE_INVALID


def test_shared_state_directory_is_refused_not_chmodded(tmp_path: Path) -> None:
    # Given an existing shared directory owned by the operator.
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    # When controller ownership is requested.
    with pytest.raises(ControllerError) as failure, BootstrapLedger(directory, HOST):
        pytest.fail("insecure directory accepted")
    # Then the path is refused and existing operator permissions stay intact.
    assert failure.value.operation == Operation.STATE_PERMISSIONS
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
