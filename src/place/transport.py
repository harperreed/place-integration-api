# ABOUTME: The async MQTT transport for Place — the SigV4 WebSocket presigner, an
# ABOUTME: MqttTransport seam over aiomqtt, and the self-healing PlaceConnection loop.
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

from .config import ALGORITHM, PATH, SCHEME, SERVICE, PlaceConfig
from .models import Credentials


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(msg: str) -> str:
    return hashlib.sha256(msg.encode("utf-8")).hexdigest().lower()


def _sign(key: bytes, msg: str) -> bytes:
    return _hmac_sha256(key, msg)


def get_signed_uri(config: PlaceConfig, credentials: Credentials) -> str:
    """Build a SigV4 presigned WSS URL for the AWS IoT MQTT WebSocket endpoint."""
    host = config.iot_endpoint
    region = config.region
    access_key_id = credentials.access_key_id
    secret_access_key = credentials.secret_access_key
    session_token = credentials.session_token

    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    credential_scope = f"{access_key_id}/{date_stamp}/{region}/{SERVICE}/aws4_request"
    signed_headers = "host"
    canonical_headers = f"host:{host}\n"
    payload_hash = _sha256_hex("")

    def enc(s: str) -> str:
        return quote(str(s), safe="")

    query = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": credential_scope,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(config.url_expire_sec),
        "X-Amz-SignedHeaders": signed_headers,
    }
    canonical_query = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query.items()))
    canonical_request = "\n".join(
        ["GET", PATH, canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    request_hash = _sha256_hex(canonical_request)
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, f"{date_stamp}/{region}/{SERVICE}/aws4_request", request_hash]
    )
    k_date = _sign(("AWS4" + secret_access_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, SERVICE)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    query["X-Amz-Signature"] = signature
    query["X-Amz-Security-Token"] = session_token
    query_string = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(query.items()))
    return f"{SCHEME}://{host}{PATH}?{query_string}"
