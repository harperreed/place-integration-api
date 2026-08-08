import dataclasses

import pytest

from place.config import PlaceConfig


def test_defaults_are_the_known_public_constants() -> None:
    cfg = PlaceConfig()
    assert cfg.region == "us-east-2"
    assert cfg.iot_endpoint == "a2ksnv5v3x6m50-ats.iot.us-east-2.amazonaws.com"
    assert cfg.cognito_user_pool_id == "us-east-2_LKSPO9tT6"
    assert cfg.cognito_client_id == "5blr1qf2evvj4ivircqbpqikev"
    assert cfg.cognito_identity_pool_id == "us-east-2:77c64042-63a1-4126-bdae-bd4150a73ad1"
    assert cfg.keep_alive_sec == 30
    assert cfg.url_expire_sec == 86400


def test_config_is_frozen() -> None:
    cfg = PlaceConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.region = "eu-west-1"  # pyright: ignore[reportAttributeAccessIssue]


def test_from_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_KEEP_ALIVE_SEC", "45")
    cfg = PlaceConfig.from_env()
    assert cfg.region == "eu-west-1"
    assert cfg.keep_alive_sec == 45
    # Untouched values fall back to defaults.
    assert cfg.cognito_client_id == "5blr1qf2evvj4ivircqbpqikev"
