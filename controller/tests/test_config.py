import json
from ipaddress import IPv4Address

import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from tests.routes import MAPPINGS
from theatre.config import ControllerConfig, KeyMapping, Mode
from theatre.keys import SAFE_KEYS


@pytest.mark.parametrize("changed", [
    {"mode": "other"},
    {"mode": "forward", "duration": None},
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
    values.update({"mode": "map", "duration": None, "reconnect": True,
                   "mappings": [m.model_dump() for m in MAPPINGS]})
    # When parsed from the same JSON format used by the container.
    actual = ControllerConfig.model_validate_json(json.dumps(values))
    # Then continuous mapping is allowed but cannot happen from default settings.
    assert actual.mode == Mode.MAP
    assert actual.duration is None
    assert actual.reconnect
    assert config.mode == Mode.OBSERVE
    assert config.duration == 30
    assert not config.reconnect


@pytest.mark.parametrize("raw", [
    '{"key":"netflix","app_id":"Other.App"}',
    '{"key":63,"app_id":"Other.App"}',
    '{"key":"home","app_id":"no-dot"}',
    '{"key":"home","app_id":"Other.App;id"}',
    '{"key":"home","app_id":"Other..App"}',
    '{"key":"home","app_id":"Other.App","code":63}',
    json.dumps({"key": "home", "app_id": "a." + "b" * 127}),
])
def test_mapping_rejects_unknown_numeric_keys_and_invalid_ids(raw: str) -> None:
    # Given untrusted user mappings, When parsed, Then unsafe values are rejected.
    with pytest.raises(ValidationError):
        _ = KeyMapping.model_validate_json(raw)


@pytest.mark.parametrize(("mode", "mappings"), [
    ("map", []), ("map", [MAPPINGS[0], MAPPINGS[0]]),
    ("observe", [MAPPINGS[0]]), ("forward", [MAPPINGS[0]]),
])
def test_mapping_policy_requires_unique_explicit_map_routes(
    config: ControllerConfig, mode: str, mappings: list[KeyMapping],
) -> None:
    # Given a mode and mappings, When validated, Then inconsistent policies fail.
    values = TypeAdapter(dict[str, JsonValue]).validate_json(config.model_dump_json())
    values.update({"mode": mode, "mappings": [m.model_dump() for m in mappings]})
    with pytest.raises(ValidationError):
        _ = ControllerConfig.model_validate_json(json.dumps(values))


def test_observe_can_reconnect_continuously(config: ControllerConfig) -> None:
    # Given observation, When continuous discovery is requested, Then no grab policy is needed.
    values = TypeAdapter(dict[str, JsonValue]).validate_json(config.model_dump_json())
    values.update({"duration": None, "reconnect": True})
    actual = ControllerConfig.model_validate_json(json.dumps(values))
    assert actual.mode == Mode.OBSERVE
    assert actual.duration is None
    assert actual.reconnect
    assert dict(SAFE_KEYS) == {"home": 63, "tv_plus": 560}
