import base64
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketListener
from samsung_tv_root.root_agent import RootAgentServer, read_secret
from samsung_tv_root.sdb import (
    SDB_APPINSTALL_MAX_SAFE_BYTES,
    SDB_APPINSTALL_PREFIX,
    build_shell_injection,
)

from theatre.bootstrap import bootstrap_command, prepare_artifacts

PACKAGE = "archive-root-0123456789abcdef"


@pytest.mark.anyio
async def test_bootstrap_is_valid_inside_the_real_sdk_wrapper() -> None:
    # Given the actual gated/backtick SDK command encoding.
    command = bootstrap_command(
        PurePosixPath("/home/owner/share/tmp/sdk_tools") / PACKAGE, PACKAGE
    )
    injection = build_shell_injection(command)
    encoded = injection.split("${IFS}")[2].split("|", 1)[0]
    script = base64.b64decode(encoded)
    # When the shell parses the exact script, without executing any command.
    with anyio.fail_after(2):
        result = await anyio.run_process(["/bin/bash", "-n"], input=script)
    # Then a trailing background operator cannot break the enclosing gate.
    assert result.returncode == 0
    assert len((SDB_APPINSTALL_PREFIX + injection).encode()) <= SDB_APPINSTALL_MAX_SAFE_BYTES


@pytest.mark.anyio
async def test_detached_payload_does_not_keep_sdk_stdout_open(tmp_path: Path) -> None:
    # Given a real long-lived child, announced through an ephemeral loopback port.
    async with await anyio.create_tcp_listener(
        local_host="127.0.0.1", local_port=0
    ) as listener:
        socket_listener = next(
            child for child in listener.listeners if isinstance(child, SocketListener)
        )
        port = listener.extra(attribute=SocketAttribute.local_port)
        executable = tmp_path / "fake dotnet"
        _ = executable.write_text(
            f"""#!{sys.executable}
import os
import socket
with socket.create_connection(("127.0.0.1", int(os.environ["ROOT_TEST_PORT"]))) as peer:
    peer.sendall(b"ready")
    peer.recv(1)
""",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        command = bootstrap_command(PurePosixPath(str(tmp_path)), PACKAGE, executable)
        async with await anyio.open_process(
            ["/bin/bash", "-c", command],
            env={"PATH": "/usr/bin:/bin", "ROOT_TEST_PORT": str(port)},
        ) as shell:
            with anyio.fail_after(2):
                async with await socket_listener.accept() as peer:
                    try:
                        assert await peer.receive() == b"ready"
                        # When the SDK shell exits while the payload is still alive.
                        assert await shell.wait() == 0
                        assert shell.stdout is not None
                        # Then EOF is observable before releasing the payload.
                        with pytest.raises(anyio.EndOfStream):
                            _ = await shell.stdout.receive()
                    finally:
                        with anyio.CancelScope(shield=True):
                            await peer.send(b"x")


@pytest.mark.anyio
async def test_archive_signature_namespace_and_private_token(tmp_path: Path) -> None:
    # Given an ephemeral local root listener, never a real TV endpoint.
    secret = bytes(range(32))
    server = RootAgentServer("127.0.0.1", 0, "127.0.0.1", secret)
    await server.start()
    try:
        # When preparing the pinned archive protocol with real RSA signing.
        await prepare_artifacts(tmp_path, PACKAGE, server)
        # Then only the intended root launch entry and private token are staged.
        assert read_secret(tmp_path / "root-agent.token") == secret
        assert stat.S_IMODE((tmp_path / "root-agent.token").stat().st_mode) == 0o600
        with tarfile.open(tmp_path / f"{PACKAGE}.tar.gz") as archive:
            assert archive.getnames() == ["launch.sh"]
            entry = archive.extractfile("launch.sh")
            assert entry is not None
            script = entry.read()
            assert secret not in script
            assert str(server.listening_port).encode() in script
        # Tampering must invalidate the independently verified signature.
        archive_path = tmp_path / f"{PACKAGE}.tar.gz"
        _ = archive_path.write_bytes(archive_path.read_bytes() + b"tampered")
        result = await anyio.run_process([
            "/usr/bin/openssl", "dgst", "-sha256", "-verify", str(tmp_path / "public.pem"),
            "-signature", str(tmp_path / f"{PACKAGE}.tar.gz.signature"), str(archive_path),
        ], check=False)
        assert result.returncode != 0
    finally:
        await server.close()
