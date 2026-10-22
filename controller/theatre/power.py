"""Read-only power gating: never use SDK/root or wake commands in standby."""

from http import HTTPStatus
from ipaddress import IPv4Address
from typing import ClassVar, Final, Literal

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_STATUS_BYTES: Final = 16384
STATUS_CHUNK_BYTES: Final = 4096
STATUS_TIMEOUT: Final = 3.0


class DevicePower(BaseModel):
    """Only the TV's explicit powered-on status authorizes root acquisition."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore", strict=True)
    state: Literal["on", "off", "standby"] = Field(alias="PowerState")


class TelevisionStatus(BaseModel):
    """Parse the native read-only Samsung device-info envelope."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore", strict=True)
    device: DevicePower


async def television_is_on(host: IPv4Address, port: int = 8001) -> bool:
    """Fail closed on off, absent, malformed, redirected or oversized status."""
    try:
        with anyio.fail_after(STATUS_TIMEOUT):
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(STATUS_TIMEOUT),
                limits=httpx2.Limits(max_connections=1, max_keepalive_connections=0),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", f"http://{host}:{port}/api/v2/") as response:
                    if response.status_code != HTTPStatus.OK:
                        return False
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=STATUS_CHUNK_BYTES):
                        if len(data) + len(chunk) > MAX_STATUS_BYTES:
                            return False
                        data.extend(chunk)
                    return TelevisionStatus.model_validate_json(bytes(data)).device.state == "on"
    except (httpx2.RequestError, ValidationError, TimeoutError):
        return False
