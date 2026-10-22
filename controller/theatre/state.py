"""Private ownership ledger for SDK stages left across TV power/network loss."""

import fcntl
import os
import stat
import tempfile
from ipaddress import IPv4Address
from pathlib import Path
from types import TracebackType
from typing import Annotated, ClassVar, Final, Self, final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from theatre.config import ControllerError, Operation

MAX_LEDGER_BYTES: Final = 65536
PackageName = Annotated[str, Field(pattern=r"^archive-root-[0-9a-f]{16}$")]


class PendingStages(BaseModel):
    """Only exact generated namespaces may become deferred deletion targets."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )
    packages: Annotated[tuple[PackageName, ...], Field(max_length=1024)] = ()


@final
class BootstrapLedger:
    """Own an exclusive controller lock and atomically updated pending stages."""

    def __init__(self, directory: Path, host: IPv4Address) -> None:
        """Keep different TVs' records and locks separate."""
        self._directory = directory
        self._path = directory / f"bootstrap-{host}.json"
        self._lock_path = directory / f"bootstrap-{host}.lock"
        self._lock: int | None = None
        self._pending = PendingStages()

    @property
    def packages(self) -> tuple[str, ...]:
        """Return the already validated, process-owned pending namespaces."""
        return self._pending.packages

    def __enter__(self) -> Self:
        """Refuse shared/insecure directories or a competing controller."""
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self._directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ControllerError(Operation.STATE_PERMISSIONS)
        descriptor = os.open(
            self._lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ControllerError(Operation.CONTROLLER_RUNNING) from None
            try:
                record = os.open(self._path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            except FileNotFoundError:
                self._pending = PendingStages()
            else:
                with os.fdopen(record, "rb") as source:
                    data = source.read(MAX_LEDGER_BYTES + 1)
                if len(data) > MAX_LEDGER_BYTES:
                    raise ControllerError(Operation.STATE_INVALID)
                try:
                    self._pending = PendingStages.model_validate_json(data)
                except ValidationError:
                    raise ControllerError(Operation.STATE_INVALID) from None
        except (OSError, ControllerError):
            os.close(descriptor)
            raise
        self._lock = descriptor
        return self

    def _save(self, pending: PendingStages) -> None:
        """Durably record ownership before any SDK stage is written."""
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self._directory, delete=False
        ) as output:
            temporary = Path(output.name)
            try:
                _ = output.write(pending.model_dump_json())
                output.flush()
                os.fsync(output.fileno())
                _ = temporary.replace(self._path)
                directory = os.open(
                    self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
        self._pending = pending

    def add(self, package: str) -> None:
        """Persist one exact namespace before staging can partially fail."""
        self._save(PendingStages(packages=(*self._pending.packages, package)))

    def forget(self, package: str) -> None:
        """Forget only after the real SDK deletion command succeeds."""
        self._save(PendingStages(packages=tuple(
            item for item in self._pending.packages if item != package
        )))

    def __exit__(
        self, _kind: type[BaseException] | None, _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Release the local lock, never remove unconfirmed TV records."""
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
