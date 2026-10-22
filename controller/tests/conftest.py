from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal

import pytest

from theatre.config import ControllerConfig


@pytest.fixture
def anyio_backend() -> Literal["asyncio"]:
    return "asyncio"


@pytest.fixture
def config() -> ControllerConfig:
    return ControllerConfig(
        tv_host=IPv4Address("127.0.0.1"),
        callback_host=IPv4Address("127.0.0.1"),
        sdb=Path("/not-a-real-sdb"),
        allow_root=True,
    )
