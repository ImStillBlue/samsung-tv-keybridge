import json
from dataclasses import dataclass
from http import HTTPStatus
from ipaddress import IPv4Address

import anyio
import pytest
from anyio.abc import SocketAttribute, SocketStream

from theatre.power import MAX_STATUS_BYTES, television_is_on


@dataclass(frozen=True, slots=True)
class Reply:
    status: HTTPStatus
    body: bytes
    redirect: bool = False


@pytest.mark.anyio
@pytest.mark.parametrize(("reply", "expected"), [
    (Reply(HTTPStatus.OK, b'{"device":{"PowerState":"on"}}'), True),
    (Reply(HTTPStatus.OK, b'{"device":{"PowerState":"off"}}'), False),
    (Reply(HTTPStatus.OK, b'{"device":{"PowerState":"standby"}}'), False),
    (Reply(HTTPStatus.OK, b'{"device":{"PowerState":"starting"}}'), False),
    (Reply(HTTPStatus.OK, b'{"device":{}}'), False),
    (Reply(HTTPStatus.OK, b"broken-json"), False),
    (Reply(HTTPStatus.SERVICE_UNAVAILABLE, b'{"device":{"PowerState":"on"}}'), False),
    (Reply(HTTPStatus.FOUND, b"", redirect=True), False),
    (Reply(
        HTTPStatus.OK,
        b'{"device":{"PowerState":"on"}}' + b" " * MAX_STATUS_BYTES,
    ), False),
])
async def test_only_explicit_bounded_power_on_is_accepted(
    reply: Reply, *, expected: bool,
) -> None:
    # Given a real HTTP peer, including a redirect that would return on if followed.
    requests: list[bytes] = []
    result: bool | None = None
    async with await anyio.create_tcp_listener(
        local_host="127.0.0.1", local_port=0
    ) as listener:
        port = listener.extra(attribute=SocketAttribute.local_port)

        async def handler(peer: SocketStream) -> None:
            async with peer:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(await peer.receive(4096))
                line = bytes(request).split(b"\r\n", 1)[0]
                requests.append(line)
                redirected = line.startswith(b"GET /wake ")
                body = (
                    json.dumps({"device": {"PowerState": "on"}}).encode()
                    if redirected else reply.body
                )
                status = HTTPStatus.OK if redirected else reply.status
                location = f"Location: http://127.0.0.1:{port}/wake\r\n" if reply.redirect else ""
                headers = (
                    f"HTTP/1.1 {status.value} {status.phrase}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"{location}Connection: close\r\n\r\n"
                ).encode()
                await peer.send(headers + body)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, handler)
            # When the production read-only power client queries this endpoint.
            result = await television_is_on(IPv4Address("127.0.0.1"), port)
            tasks.cancel_scope.cancel()
    # Then no root/wake method or redirected request was issued.
    assert result is expected
    assert requests == [b"GET /api/v2/ HTTP/1.1"]


@pytest.mark.anyio
async def test_status_transport_failure_is_not_power_on() -> None:
    # Given a real peer that closes without an HTTP status.
    result: bool | None = None
    async with await anyio.create_tcp_listener(
        local_host="127.0.0.1", local_port=0
    ) as listener:
        port = listener.extra(attribute=SocketAttribute.local_port)

        async def close(peer: SocketStream) -> None:
            await peer.aclose()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, close)
            # When the request loses its transport.
            result = await television_is_on(IPv4Address("127.0.0.1"), port)
            tasks.cancel_scope.cancel()
    # Then SDK/root must remain gated off.
    assert result is False
