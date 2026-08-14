# ABOUTME: PlaceConfig — the SDK's frozen, typed configuration for the PLACE cloud (AWS/Cognito/
# ABOUTME: IoT endpoints, reconnect + refresh tuning), with real PLACE defaults and from_env() overrides.
from __future__ import annotations

from dataclasses import dataclass

import decouple

REGION = "us-east-2"
SERVICE = "iotdevicegateway"
ALGORITHM = "AWS4-HMAC-SHA256"
SCHEME = "wss"
PATH = "/mqtt"
EXPIRE_SEC = 86400
KEEP_ALIVE_SEC = 30
FULFILLMENT_URL = (
    "https://14kbj32umd.execute-api.us-east-1.amazonaws.com/prod/fulfillment"
)
# Note: must be the regional IoT endpoint for now. Currently the custom IoT endpoint/domain configuration uses ApplicationProtocol.SECURE_MQTT
# a second domain configuration must be created for ApplicationProtocol.MQTT_WSS in order to use the custom IoT endpoint instead.
IOT_ENDPOINT = "a2ksnv5v3x6m50-ats.iot.us-east-2.amazonaws.com"
COGNITO_USER_POOL_ID = "us-east-2_LKSPO9tT6"
COGNITO_CLIENT_ID = "5blr1qf2evvj4ivircqbpqikev"
COGNITO_IDENTITY_POOL_ID = "us-east-2:77c64042-63a1-4126-bdae-bd4150a73ad1"
OAUTH2_TOKEN_URL = "https://auth.connectedsmoke.com/oauth2/token"


@dataclass(frozen=True)
class PlaceConfig:
    """Injectable Place configuration. Defaults are the public PLACE app constants."""

    region: str = "us-east-2"
    iot_endpoint: str = "a2ksnv5v3x6m50-ats.iot.us-east-2.amazonaws.com"
    cognito_user_pool_id: str = "us-east-2_LKSPO9tT6"
    cognito_client_id: str = "5blr1qf2evvj4ivircqbpqikev"
    cognito_identity_pool_id: str = "us-east-2:77c64042-63a1-4126-bdae-bd4150a73ad1"
    fulfillment_url: str = (
        "https://14kbj32umd.execute-api.us-east-1.amazonaws.com/prod/fulfillment"
    )
    oauth2_token_url: str = "https://auth.connectedsmoke.com/oauth2/token"
    keep_alive_sec: int = 30
    url_expire_sec: int = 86400
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 60.0
    token_refresh_margin_sec: int = 300
    creds_refresh_margin_sec: int = 600

    @classmethod
    def from_env(cls) -> "PlaceConfig":
        """Build a config, letting environment variables override defaults."""
        defaults = cls()
        return cls(
            region=str(decouple.config("AWS_REGION", default=defaults.region)),
            iot_endpoint=str(
                decouple.config("AWS_IOT_ENDPOINT", default=defaults.iot_endpoint)
            ),
            cognito_user_pool_id=str(
                decouple.config(
                    "AWS_COGNITO_USER_POOL_ID",
                    default=defaults.cognito_user_pool_id,
                )
            ),
            cognito_client_id=str(
                decouple.config(
                    "AWS_COGNITO_CLIENT_ID", default=defaults.cognito_client_id
                )
            ),
            cognito_identity_pool_id=str(
                decouple.config(
                    "AWS_COGNITO_IDENTITY_POOL_ID",
                    default=defaults.cognito_identity_pool_id,
                )
            ),
            fulfillment_url=str(
                decouple.config("AWS_FULFILLMENT_URL", default=defaults.fulfillment_url)
            ),
            oauth2_token_url=str(
                decouple.config("OAUTH2_TOKEN_URL", default=defaults.oauth2_token_url)
            ),
            keep_alive_sec=int(
                decouple.config(
                    "AWS_KEEP_ALIVE_SEC", default=defaults.keep_alive_sec, cast=int
                )
            ),
            url_expire_sec=int(
                decouple.config(
                    "AWS_EXPIRE_SEC", default=defaults.url_expire_sec, cast=int
                )
            ),
        )
