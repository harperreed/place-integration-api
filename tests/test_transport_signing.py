from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from place.config import PlaceConfig
from place.models import Credentials
from place.transport import get_signed_uri


def _creds() -> Credentials:
    return Credentials(
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG",
        session_token="SESSIONTOKEN",
        identity_id="us-east-2:idid",
    )


def test_signed_uri_is_a_presigned_wss_url() -> None:
    cfg = PlaceConfig()
    uri = get_signed_uri(cfg, _creds())

    assert uri.startswith(f"wss://{cfg.iot_endpoint}/mqtt?")
    q = parse_qs(urlsplit(uri).query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == [str(cfg.url_expire_sec)]
    assert q["X-Amz-Security-Token"] == ["SESSIONTOKEN"]
    assert q["X-Amz-SignedHeaders"] == ["host"]
    assert "X-Amz-Signature" in q
    cred = q["X-Amz-Credential"][0]
    assert cred.startswith("AKIDEXAMPLE/")
    assert f"{cfg.region}/iotdevicegateway/aws4_request" in cred


def test_region_flows_into_the_credential_scope() -> None:
    cfg = PlaceConfig(region="eu-west-1")
    q = parse_qs(urlsplit(get_signed_uri(cfg, _creds())).query)
    assert "eu-west-1/iotdevicegateway/aws4_request" in q["X-Amz-Credential"][0]
