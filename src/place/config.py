# ABOUTME: PlaceConfig — the SDK's frozen, typed configuration for the PLACE cloud (AWS/Cognito/
# ABOUTME: IoT endpoints, reconnect + refresh tuning), with real PLACE defaults and from_env() overrides.
from __future__ import annotations

from dataclasses import dataclass

import decouple

REGION: str = str(decouple.config("AWS_REGION", default="us-east-2"))
SERVICE: str = str(decouple.config("AWS_SERVICE", default="iotdevicegateway"))
ALGORITHM: str = str(decouple.config("AWS_ALGORITHM", default="AWS4-HMAC-SHA256"))
SCHEME: str = str(decouple.config("AWS_SCHEME", default="wss"))
PATH: str = str(decouple.config("AWS_PATH", default="/mqtt"))
EXPIRE_SEC: int = int(decouple.config("AWS_EXPIRE_SEC", default=86400))
KEEP_ALIVE_SEC: int = int(decouple.config("AWS_KEEP_ALIVE_SEC", default=30))
FULFILLMENT_URL: str = str(
    decouple.config(
        "AWS_FULFILLMENT_URL",
        default="https://14kbj32umd.execute-api.us-east-1.amazonaws.com/prod/fulfillment",
    )
)
# Note: must be the regional IoT endpoint for now. Currently the custom IoT endpoint/domain configuration uses ApplicationProtocol.SECURE_MQTT
# a second domain configuration must be created for ApplicationProtocol.MQTT_WSS in order to use the custom IoT endpoint instead.
IOT_ENDPOINT: str = str(
    decouple.config(
        "AWS_IOT_ENDPOINT", default="a2ksnv5v3x6m50-ats.iot.us-east-2.amazonaws.com"
    )
)
COGNITO_USER_POOL_ID: str = str(
    decouple.config("AWS_COGNITO_USER_POOL_ID", default="us-east-2_LKSPO9tT6")
)
COGNITO_CLIENT_ID: str = str(
    decouple.config("AWS_COGNITO_CLIENT_ID", default="5blr1qf2evvj4ivircqbpqikev")
)
COGNITO_IDENTITY_POOL_ID: str = str(
    decouple.config(
        "AWS_COGNITO_IDENTITY_POOL_ID",
        default="us-east-2:77c64042-63a1-4126-bdae-bd4150a73ad1",
    )
)
OAUTH2_TOKEN_URL: str = str(
    decouple.config(
        "OAUTH2_TOKEN_URL", default="https://auth.connectedsmoke.com/oauth2/token"
    )
)


@dataclass(frozen=True)
class PlaceConfig:
    """Injectable Place configuration. Defaults are the public PLACE app constants."""

    region: str = str(REGION)
    iot_endpoint: str = str(IOT_ENDPOINT)
    cognito_user_pool_id: str = str(COGNITO_USER_POOL_ID)
    cognito_client_id: str = str(COGNITO_CLIENT_ID)
    cognito_identity_pool_id: str = str(COGNITO_IDENTITY_POOL_ID)
    fulfillment_url: str = str(FULFILLMENT_URL)
    oauth2_token_url: str = str(OAUTH2_TOKEN_URL)
    keep_alive_sec: int = int(KEEP_ALIVE_SEC)
    url_expire_sec: int = int(EXPIRE_SEC)
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 60.0
    token_refresh_margin_sec: int = 300
    creds_refresh_margin_sec: int = 600

    @classmethod
    def from_env(cls) -> "PlaceConfig":
        """Build a config, letting environment variables override defaults."""
        return cls(
            region=str(decouple.config("AWS_REGION", default=REGION)),
            iot_endpoint=str(decouple.config("AWS_IOT_ENDPOINT", default=IOT_ENDPOINT)),
            cognito_user_pool_id=str(
                decouple.config("AWS_COGNITO_USER_POOL_ID", default=COGNITO_USER_POOL_ID)
            ),
            cognito_client_id=str(
                decouple.config("AWS_COGNITO_CLIENT_ID", default=COGNITO_CLIENT_ID)
            ),
            cognito_identity_pool_id=str(
                decouple.config(
                    "AWS_COGNITO_IDENTITY_POOL_ID", default=COGNITO_IDENTITY_POOL_ID
                )
            ),
            fulfillment_url=str(
                decouple.config("AWS_FULFILLMENT_URL", default=FULFILLMENT_URL)
            ),
            oauth2_token_url=str(
                decouple.config("OAUTH2_TOKEN_URL", default=OAUTH2_TOKEN_URL)
            ),
            keep_alive_sec=int(
                decouple.config("AWS_KEEP_ALIVE_SEC", default=KEEP_ALIVE_SEC, cast=int)
            ),
            url_expire_sec=int(
                decouple.config("AWS_EXPIRE_SEC", default=EXPIRE_SEC, cast=int)
            ),
        )
