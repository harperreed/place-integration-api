# ABOUTME: Tests for the SDK's typed error taxonomy — the class hierarchy and the
# ABOUTME: MfaRequired payload (challenge_name / session / username) consumers read.
from place.exceptions import (
    MfaRequired,
    PlaceAuthError,
    PlaceConnectionError,
    PlaceDiscoveryError,
    PlaceError,
    PlaceTimeoutError,
)


def test_hierarchy() -> None:
    for exc in (
        PlaceAuthError,
        PlaceConnectionError,
        PlaceDiscoveryError,
        PlaceTimeoutError,
    ):
        assert issubclass(exc, PlaceError)
    assert issubclass(MfaRequired, PlaceAuthError)


def test_auth_failure_hierarchy_distinguishes_user_action_from_retry() -> None:
    from place.exceptions import PlaceInvalidAuthError, PlaceTransientAuthError

    assert issubclass(PlaceInvalidAuthError, PlaceAuthError)
    assert issubclass(PlaceTransientAuthError, PlaceAuthError)


def test_mfa_required_carries_challenge() -> None:
    exc = MfaRequired(
        challenge_name="SOFTWARE_TOKEN_MFA", session="sess-123", username="alice"
    )
    assert exc.challenge_name == "SOFTWARE_TOKEN_MFA"
    assert exc.session == "sess-123"
    assert exc.username == "alice"
    assert isinstance(exc, PlaceError)
