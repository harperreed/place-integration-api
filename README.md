<!-- ABOUTME: Explains how to install, authenticate, and consume the PLACE Python SDK. -->
<!-- ABOUTME: Defines the supported read-only boundary and the local release checks. -->

# PLACE integration API

An async Python SDK for discovering Gentex PLACE devices and reading their live MQTT
state. The current client is read-only: it subscribes to device data and publishes only
AWS IoT shadow/get requests.

## Install

```sh
python -m pip install place-integration-api
```

The SDK requires Python 3.11 or newer.

## Interactive authentication

Use SRP with the account password during interactive setup. Cognito may require an MFA
code before it issues tokens.

```python
import aiohttp

from place import CognitoAuth, FileTokenCache, MfaRequired, PlaceConfig

config = PlaceConfig()
cache = FileTokenCache.default()

async with aiohttp.ClientSession() as session:
    auth = CognitoAuth(config, session, token_cache=cache)
    try:
        await auth.authenticate(username, password)
    except MfaRequired:
        await auth.submit_mfa(mfa_code)
```

The cache stores a refresh token as an owner-only file. Treat it as a password: keep it
out of source control and logs.

## Stored-session startup

Once interactive setup has saved a refresh token, start later processes without keeping
the account password. Cache-only authentication never falls back to SRP.

```python
import aiohttp

from place import CognitoAuth, FileTokenCache, PlaceConfig

config = PlaceConfig()

async with aiohttp.ClientSession() as session:
    auth = CognitoAuth(config, session, token_cache=FileTokenCache.default())
    await auth.authenticate_from_cache(username)
```

`PlaceInvalidAuthError` means the stored credentials need user action. Other
`PlaceAuthError` values may describe a temporary failure that a caller can retry. Error
messages are safe to log, but usernames, passwords, MFA codes, tokens, and raw service
responses are not.

## Discovery and runtime errors

```python
from place import PlaceClient, PlaceConfig, PlaceError


def handle_place_error(error: PlaceError) -> None:
    print(type(error).__name__)


# Use the authenticated auth instance from either flow above.
client = PlaceClient.create(PlaceConfig(), auth)
devices = await client.async_discover()
unsubscribe = client.on_error(handle_place_error)

try:
    async with client:
        # Read client.devices and register update, event, or connection listeners here.
        ...
finally:
    unsubscribe()
```

`async_discover()` reads the account device list without starting MQTT. Entering the
client starts subscriptions and shadow/get requests; it does not expose device controls.

## Development

Install the locked development environment and run the same checks as CI:

```sh
uv sync --locked --extra dev
scripts/check
```

The canonical check checks formatting, lints, type-checks, tests, builds, and validates
both release artifacts. Releases must use a tag that matches the version in
`pyproject.toml`. After publishing, CI requires PyPI's file set and SHA-256 digests to
match the local wheel and sdist exactly; an unexpected remote file fails verification.
