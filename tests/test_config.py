# ABOUTME: Verifies PLACE defaults are stable unless callers opt into environment config.
# ABOUTME: Import-isolated tests guard against ambient process and dotenv contamination.
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from place import config as config_module
from place.config import PlaceConfig

CONFIG_ENV = {
    "AWS_REGION": "canary-region",
    "AWS_SERVICE": "canary-service",
    "AWS_ALGORITHM": "canary-algorithm",
    "AWS_SCHEME": "canary-scheme",
    "AWS_PATH": "canary-path",
    "AWS_EXPIRE_SEC": "111",
    "AWS_KEEP_ALIVE_SEC": "222",
    "AWS_FULFILLMENT_URL": "canary-fulfillment",
    "AWS_IOT_ENDPOINT": "canary-iot",
    "AWS_COGNITO_USER_POOL_ID": "canary-user-pool",
    "AWS_COGNITO_CLIENT_ID": "canary-client",
    "AWS_COGNITO_IDENTITY_POOL_ID": "canary-identity-pool",
    "OAUTH2_TOKEN_URL": "canary-oauth",
}

PLACE_DEFAULTS = {
    "region": "us-east-2",
    "iot_endpoint": "a2ksnv5v3x6m50-ats.iot.us-east-2.amazonaws.com",
    "cognito_user_pool_id": "us-east-2_LKSPO9tT6",
    "cognito_client_id": "5blr1qf2evvj4ivircqbpqikev",
    "cognito_identity_pool_id": "us-east-2:77c64042-63a1-4126-bdae-bd4150a73ad1",
    "fulfillment_url": (
        "https://14kbj32umd.execute-api.us-east-1.amazonaws.com/prod/fulfillment"
    ),
    "oauth2_token_url": "https://auth.connectedsmoke.com/oauth2/token",
    "keep_alive_sec": 30,
    "url_expire_sec": 86400,
    "reconnect_min_sec": 1.0,
    "reconnect_max_sec": 60.0,
    "token_refresh_margin_sec": 300,
    "creds_refresh_margin_sec": 600,
}

TRANSPORT_DEFAULTS = {
    "service": "iotdevicegateway",
    "algorithm": "AWS4-HMAC-SHA256",
    "scheme": "wss",
    "path": "/mqtt",
}

SUBPROCESS_SCRIPT = """
import dataclasses
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("isolated_place_config", sys.argv[1])
if spec is None or spec.loader is None:
    raise RuntimeError("could not load isolated config module")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(json.dumps({
    "plain": dataclasses.asdict(module.PlaceConfig()),
    "explicit": dataclasses.asdict(module.PlaceConfig.from_env()),
    "transport": {
        "service": module.SERVICE,
        "algorithm": module.ALGORITHM,
        "scheme": module.SCHEME,
        "path": module.PATH,
    },
}, sort_keys=True))
"""


def _run_isolated_config(
    tmp_path: Path,
    *,
    environment: dict[str, str],
    dotenv: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Import a private copy of config.py with controlled ambient configuration."""
    case_dir = tmp_path / ("dotenv" if dotenv else "environment")
    case_dir.mkdir()
    config_path = case_dir / "config.py"
    shutil.copyfile(Path(__file__).parents[1] / "src/place/config.py", config_path)
    if dotenv:
        (case_dir / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in dotenv.items())
        )

    process_environment = os.environ.copy()
    for key in CONFIG_ENV:
        process_environment.pop(key, None)
    process_environment.update(environment)
    completed = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_SCRIPT, str(config_path)],
        check=True,
        capture_output=True,
        text=True,
        env=process_environment,
    )
    result: dict[str, Any] = json.loads(completed.stdout)
    return result


def test_defaults_are_the_known_public_constants() -> None:
    cfg = PlaceConfig()
    assert cfg.region == "us-east-2"
    assert cfg.iot_endpoint == "a2ksnv5v3x6m50-ats.iot.us-east-2.amazonaws.com"
    assert cfg.cognito_user_pool_id == "us-east-2_LKSPO9tT6"
    assert cfg.cognito_client_id == "5blr1qf2evvj4ivircqbpqikev"
    assert (
        cfg.cognito_identity_pool_id == "us-east-2:77c64042-63a1-4126-bdae-bd4150a73ad1"
    )
    assert cfg.keep_alive_sec == 30
    assert cfg.url_expire_sec == 86400


def test_dataclass_defaults_match_public_module_constants() -> None:
    field_defaults = {
        field.name: field.default for field in dataclasses.fields(PlaceConfig)
    }

    assert field_defaults["region"] == config_module.REGION
    assert field_defaults["iot_endpoint"] == config_module.IOT_ENDPOINT
    assert field_defaults["cognito_user_pool_id"] == config_module.COGNITO_USER_POOL_ID
    assert field_defaults["cognito_client_id"] == config_module.COGNITO_CLIENT_ID
    assert (
        field_defaults["cognito_identity_pool_id"]
        == config_module.COGNITO_IDENTITY_POOL_ID
    )
    assert field_defaults["fulfillment_url"] == config_module.FULFILLMENT_URL
    assert field_defaults["oauth2_token_url"] == config_module.OAUTH2_TOKEN_URL
    assert field_defaults["keep_alive_sec"] == config_module.KEEP_ALIVE_SEC
    assert field_defaults["url_expire_sec"] == config_module.EXPIRE_SEC


def test_public_module_constants_keep_runtime_type_annotations() -> None:
    annotations = get_type_hints(config_module)

    assert annotations == {
        "REGION": str,
        "SERVICE": str,
        "ALGORITHM": str,
        "SCHEME": str,
        "PATH": str,
        "EXPIRE_SEC": int,
        "KEEP_ALIVE_SEC": int,
        "FULFILLMENT_URL": str,
        "IOT_ENDPOINT": str,
        "COGNITO_USER_POOL_ID": str,
        "COGNITO_CLIENT_ID": str,
        "COGNITO_IDENTITY_POOL_ID": str,
        "OAUTH2_TOKEN_URL": str,
    }


def test_plain_config_ignores_environment_and_dotenv(tmp_path: Path) -> None:
    ambient = _run_isolated_config(tmp_path, environment=CONFIG_ENV)
    dotenv = _run_isolated_config(tmp_path, environment={}, dotenv=CONFIG_ENV)

    assert ambient["plain"] == PLACE_DEFAULTS
    assert ambient["transport"] == TRANSPORT_DEFAULTS
    assert dotenv["plain"] == PLACE_DEFAULTS
    assert dotenv["transport"] == TRANSPORT_DEFAULTS


def test_from_env_is_the_only_environment_opt_in(tmp_path: Path) -> None:
    ambient = _run_isolated_config(tmp_path, environment=CONFIG_ENV)
    dotenv = _run_isolated_config(tmp_path, environment={}, dotenv=CONFIG_ENV)

    expected = {
        **PLACE_DEFAULTS,
        "region": "canary-region",
        "iot_endpoint": "canary-iot",
        "cognito_user_pool_id": "canary-user-pool",
        "cognito_client_id": "canary-client",
        "cognito_identity_pool_id": "canary-identity-pool",
        "fulfillment_url": "canary-fulfillment",
        "oauth2_token_url": "canary-oauth",
        "keep_alive_sec": 222,
        "url_expire_sec": 111,
    }
    assert ambient["explicit"] == expected
    assert dotenv["explicit"] == expected


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
