# ABOUTME: Pure helpers for the PLACE MQTT layer — topic builders (shadow + household),
# ABOUTME: payload parsing, message classification, and thing-name extraction.
from __future__ import annotations

import json
from typing import Any, Mapping

HOUSEHOLD_PREFIX = "connectedsmoke/household"
SHADOW_GET_PREFIX = "$aws/things"


def parse_payload(raw: bytes) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def message_kind(topic: str, payload: dict[str, Any]) -> str:
    checks = [
        (lambda: "state" in payload and payload.get("state", {}).get("reported") is not None, "shadow"),
        (lambda: "connectivity" in topic.lower(), "presence"),
        (lambda: "command/response" in topic, "command"),
        (lambda: "events/" in topic, "event"),
        (lambda: topic.startswith("connectedsmoke/household/"), "household"),
    ]
    return next((label for pred, label in checks if pred()), "msg")


def household_subscription_topic(household_id: str) -> str:
    return f"{HOUSEHOLD_PREFIX}/{household_id}/#"


def shadow_get_topic(thing_name: str) -> str:
    return f"{SHADOW_GET_PREFIX}/{thing_name}/shadow/get"


def shadow_subscription_topic(thing_name: str) -> str:
    return f"{SHADOW_GET_PREFIX}/{thing_name}/shadow/#"


def desired_shadow_update(thing_name: str, fields: Mapping[str, Any]) -> tuple[str, str]:
    """Build a standard AWS IoT shadow-update message that writes ``desired`` state.

    This is the write side of the shadow transport these helpers already read. It
    is schema-agnostic — it wraps whatever desired fields the caller supplies; the
    concrete per-command field names are device-specific and intentionally not
    baked in here. It only *builds* the (topic, payload) pair — nothing is
    published, so importing it changes no device state.
    """
    topic = f"{SHADOW_GET_PREFIX}/{thing_name}/shadow/update"
    payload = json.dumps({"state": {"desired": dict(fields)}})
    return topic, payload


def describe_message(topic: str, raw: bytes) -> str:
    payload = parse_payload(raw)
    kind = message_kind(topic, payload)
    extra = (
        f" -> reported: {list(payload.get('state', {}).get('reported', {}).keys())}"
        if kind == "shadow"
        else f" {payload}"
        if kind == "msg"
        else ""
    )
    return f"[{kind}] {topic}{extra}"


def thing_name_from_topic(topic: str) -> str | None:
    """Extract thing_name from an AWS IoT shadow topic ($aws/things/{name}/shadow/...)."""
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "$aws" and parts[1] == "things":
        return parts[2]
    return None


def household_id_from_thing_name(thing_name: str) -> str:
    """Return the household id encoded in a thing name's leading token.

    A thing name is ``{householdId}_{registrationId}_{deviceId}``. The household
    and registration ids are UUIDs (no underscores), so the first underscore
    always terminates the household id — even though the deviceId itself contains
    underscores (``Place_PL1AS_xxxx``). This lets the client derive the household
    subscription (which carries live motion events) straight from discovery,
    with no separately configured household id.
    """
    return thing_name.split("_", 1)[0]
