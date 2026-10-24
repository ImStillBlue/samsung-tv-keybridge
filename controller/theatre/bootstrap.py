"""Owned SDK archive bootstrap, adapted from samsung-tv-root 4ad2c4d3b0d0.

The upstream Unlicense implementation is preserved at the protocol boundary.
Samsung runtime discovery and detached-shell ownership are specific to this TV.
No private upstream Python functions are imported.
"""

import io
import json
import logging
import re
import secrets
import shlex
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Final

import anyio
from anyio.to_thread import run_sync
from samsung_tv_root.archive_root import (
    PAYLOAD_FILES,
    RUNTIME_CONFIG_FILES,
    STAGING_ROOT,
    SYSTEM_HASH_COMMAND,
    TargetAssessment,
)
from samsung_tv_root.root_agent import RootAgentServer, write_secret
from samsung_tv_root.sdb import SdbClient

from theatre.config import ControllerConfig, ControllerError, Operation, TelevisionUnavailableError
from theatre.power import television_is_on
from theatre.state import BootstrapLedger

DOTNET_EXECUTABLE: Final = Path("/usr/bin/dotnet")
logger: Final = logging.getLogger("theatre")


def capture(client: SdbClient, command: str) -> str:
    """Require the SDK callback's real exit marker, not appinstall's exit code."""
    result = client.capture(command, timeout=client.timeout)
    if "\n[exit:0]" not in result.output:
        raise ControllerError(Operation.ROOT_TRANSPORT)
    return result.output.split("\n[exit:0]", 1)[0].strip()


def assess(client: SdbClient) -> TargetAssessment:
    """Check archive capabilities and readable Samsung .NET 3.1 runtime files."""
    client.connect()
    client.require_device()
    client.require_shell_injection()
    capabilities = capture(client, "getcap /usr/bin/dotnet /usr/sbin/sdbd-tarlauncher")
    for path, required in (
        ("/usr/bin/dotnet", ("cap_setgid", "cap_sys_admin")),
        ("/usr/sbin/sdbd-tarlauncher", ("cap_setgid", "cap_setuid")),
    ):
        line = next((line for line in capabilities.splitlines() if path in line), "")
        flags = {
            name: match.group(2)
            for match in re.finditer(r"(cap_[a-z0-9_,]+)[=+]([eip]+)", line)
            for name in match.group(1).split(",")
        }
        if not all("e" in flags.get(name, "") for name in required):
            raise ControllerError(Operation.ROOT_CAPABILITIES)
    runtimes = capture(
        client,
        """for r in /usr/share/dotnet/shared/Microsoft.NETCore.App/3.1.*;do
test -r "$r/libcoreclr.so" -a -r "$r/System.Private.CoreLib.dll"
&& echo "${r##*/}";done;true""".replace("\n", " "),
    )
    if not any(re.fullmatch(r"3\.1\.\d+", line) for line in runtimes.splitlines()):
        raise ControllerError(Operation.ROOT_RUNTIME)
    if "--to-command" not in capture(client, "/usr/bin/tar --help"):
        raise ControllerError(Operation.ROOT_TAR)
    return TargetAssessment(
        capabilities, capture(client, SYSTEM_HASH_COMMAND), "3.1"
    )


def _write_archive(directory: Path, package: str, server: RootAgentServer) -> None:
    """Write only this run's runtime metadata, token and single-entry archive."""
    staging = STAGING_ROOT / package
    runtime = {
        "runtimeOptions": {
            "tfm": "netcoreapp3.1",
            "rollForward": "LatestPatch",
            "framework": {"name": "Microsoft.NETCore.App", "version": "3.1.0"},
        }
    }
    for name in RUNTIME_CONFIG_FILES:
        _ = (directory / name).write_text(json.dumps(runtime) + "\n", encoding="ascii")
    _ = (directory / "passwd").write_text(
        "owner:x:0:0:owner:/home/owner:/bin/sh\n", encoding="ascii"
    )
    write_secret(directory / "root-agent.token", server.secret)
    script = (
        "#!/bin/sh\n"
        f"exec /usr/bin/dotnet {staging}/SamsungTvRootAgent.dll "
        f"{server.bind_host} {server.listening_port} "
        f"{staging}/root-agent.token {staging}\n"
    ).encode("ascii")
    with tarfile.open(directory / f"{package}.tar.gz", "w:gz") as output:
        entry = tarfile.TarInfo("launch.sh")
        entry.size, entry.mode, entry.mtime = len(script), 0o600, 0
        output.addfile(entry, io.BytesIO(script))


async def prepare_artifacts(
    directory: Path, package: str, server: RootAgentServer
) -> None:
    """Create and verify the same RSA-SHA256 archive signature as upstream."""
    await run_sync(_write_archive, directory, package, server)
    private, public = directory / "private.pem", directory / "public.pem"
    archive = directory / f"{package}.tar.gz"
    signature = directory / f"{package}.tar.gz.signature"
    with anyio.fail_after(15):
        _ = await anyio.run_process([
            "/usr/bin/openssl", "genpkey", "-algorithm", "RSA",
            "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private),
        ])
        _ = await anyio.run_process([
            "/usr/bin/openssl", "pkey", "-in", str(private),
            "-pubout", "-out", str(public),
        ])
        _ = await anyio.run_process([
            "/usr/bin/openssl", "dgst", "-sha256", "-sign", str(private),
            "-out", str(signature), str(archive),
        ])
        _ = await anyio.run_process([
            "/usr/bin/openssl", "dgst", "-sha256", "-verify", str(public),
            "-signature", str(signature), str(archive),
        ])


def stage(client: SdbClient, directory: Path, payloads: Path) -> None:
    """Stage one package; its name comes from the generated archive filename."""
    archive = next(directory.glob("archive-root-*.tar.gz"))
    package = archive.name.removesuffix(".tar.gz")
    staging = STAGING_ROOT / package
    on_demand = STAGING_ROOT / "on-demand"
    _ = capture(client, f"/bin/mkdir -p {staging} {on_demand}")
    for name in PAYLOAD_FILES:
        client.push(payloads / name, staging / name)
    for name in (*RUNTIME_CONFIG_FILES, "public.pem", "passwd", "root-agent.token"):
        client.push(directory / name, staging / name)
    client.push(archive, on_demand / archive.name)
    client.push(directory / f"{archive.name}.signature", on_demand / f"{archive.name}.signature")


def cleanup(client: SdbClient, package: str) -> None:
    """Remove only the unique bootstrap directory and its two archive files."""
    staging = STAGING_ROOT / package
    archive = STAGING_ROOT / "on-demand" / f"{package}.tar.gz"
    _ = capture(
        client,
        f"/bin/rm -rf {staging} && /bin/rm -f {archive} {archive}.signature",
    )


def bootstrap_command(
    staging: PurePosixPath, package: str, executable: Path = DOTNET_EXECUTABLE
) -> str:
    """Detach the executable, not a shell AND-list retaining the SDK's pipes."""
    return (
        f"cd {shlex.quote(str(staging))} && {{ "
        f"nohup {shlex.quote(str(executable))} SamsungTvArchiveRoot.dll {shlex.quote(package)} "
        ">bootstrap.log 2>&1 </dev/null 3>&- & :; }"
    )


@asynccontextmanager
async def sdk_bootstrap(
    config: ControllerConfig, client: SdbClient, server: RootAgentServer
) -> AsyncIterator[str]:
    """Own a generated SDK stage until the authenticated root lease ends."""
    package = "archive-root-" + secrets.token_hex(8)
    staged = False
    with (
        BootstrapLedger(config.state_dir, config.tv_host) as ledger,
        tempfile.TemporaryDirectory(prefix="theatre-bootstrap-") as temp,
    ):
        for pending in ledger.packages:
            if not await television_is_on(config.tv_host):
                raise TelevisionUnavailableError
            await run_sync(cleanup, client, pending)
            await run_sync(ledger.forget, pending)
        directory = Path(temp)
        await prepare_artifacts(directory, package, server)
        try:
            await run_sync(ledger.add, package)
            staged = True
            await run_sync(stage, client, directory, config.payloads)
            yield bootstrap_command(STAGING_ROOT / package, package)
        finally:
            if staged:
                with anyio.CancelScope(shield=True):
                    if await television_is_on(config.tv_host):
                        await run_sync(cleanup, client, package)
                        await run_sync(ledger.forget, package)
                    else:
                        logger.warning("bootstrap.cleanup_deferred package=%s", package)
