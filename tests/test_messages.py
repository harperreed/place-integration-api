"""Tests for the message/topic helpers in place.messages."""

import json

from place.messages import desired_shadow_update


def test_desired_shadow_update_wraps_fields_under_state_desired() -> None:
    """The write helper produces a standard AWS IoT shadow-update message.

    It is schema-agnostic: it wraps whatever desired fields the caller supplies.
    The exact per-command field names are device-specific and are NOT asserted
    here. The helper only *builds* the message — nothing is published.
    """
    topic, payload = desired_shadow_update("thing-abc", {"exampleField": 1})

    assert topic == "$aws/things/thing-abc/shadow/update"
    assert json.loads(payload) == {"state": {"desired": {"exampleField": 1}}}
