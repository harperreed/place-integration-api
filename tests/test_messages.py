"""Tests for the message/topic helpers in place.messages."""

import json

from place.messages import desired_shadow_update, household_id_from_thing_name


def test_desired_shadow_update_wraps_fields_under_state_desired() -> None:
    """The write helper produces a standard AWS IoT shadow-update message.

    It is schema-agnostic: it wraps whatever desired fields the caller supplies.
    The exact per-command field names are device-specific and are NOT asserted
    here. The helper only *builds* the message — nothing is published.
    """
    topic, payload = desired_shadow_update("thing-abc", {"exampleField": 1})

    assert topic == "$aws/things/thing-abc/shadow/update"
    assert json.loads(payload) == {"state": {"desired": {"exampleField": 1}}}


def test_household_id_from_thing_name_returns_leading_token() -> None:
    """The household id is a thing name's first underscore-delimited token.

    A thing name is ``{householdId}_{registrationId}_{deviceId}``; the two ids
    are UUIDs (which contain no underscore), so splitting on the first
    underscore isolates the household even though the deviceId itself contains
    underscores (``Place_PL1AS_xxxx``).
    """
    thing = "hh-uuid_reg-uuid_Place_PL1AS_EXAMPLE"
    assert household_id_from_thing_name(thing) == "hh-uuid"
