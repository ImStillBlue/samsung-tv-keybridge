import json
from ipaddress import IPv4Address

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from theatre.config import ControllerConfig, Mode


@pytest.mark.parametrize("changed", [
    {"mode": "other"},
    {"mode": "observe", "duration": None},
    {"mode": "forward", "duration": None},
    {"mode": "observe", "reconnect": True},
    {"mode": "forward", "reconnect": True},
    {"duration": 0},
    {"duration": 31},
    {"allow_root": "true"},
    {"tv_host": "localhost"},
    {"tv_host": "127.1"},
    {"callback_host": "::1"},
    {"callback_host": str(IPv4Address(0))},
    {"callback_host": "224.0.0.1"},
    {"tv_host": "255.255.255.255"},
    {"home_app_id": "Other.App"},
    {"token": "must-not-be-a-host-setting"},
])
def test_invalid_or_unbounded_diagnostics_are_rejected(
    config: ControllerConfig, changed: dict[str, str | int | None],
) -> None:
    # Given an otherwise valid operator configuration.
    values = TypeAdapter(dict[str, JsonValue]).validate_json(config.model_dump_json())
    values.update(changed)
    # When unsafe modes, implicit coercion or extra routing fields cross the boundary.
    with pytest.raises(ValidationError):
        _ = ControllerConfig.model_validate_json(json.dumps(values))
    # Then no runtime/network activity can start.


def test_continuous_mapping_is_explicit(config: ControllerConfig) -> None:
    # Given a manual mapping opt-in rather than a diagnostic mode.
    values = TypeAdapter(dict[str, JsonValue]).validate_json(config.model_dump_json())
    values.update({"mode": "map", "duration": None, "reconnect": True})
    # When parsed from the same JSON format used by the container.
    actual = ControllerConfig.model_validate_json(json.dumps(values))
    # Then continuous mapping is allowed but cannot happen from default settings.
    assert actual.mode == Mode.MAP
    assert actual.duration is None
    assert actual.reconnect
    assert config.mode == Mode.OBSERVE
    assert config.duration == 30
    assert not config.reconnect
