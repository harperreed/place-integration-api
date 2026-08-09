# ABOUTME: FileTokenCache — best-effort on-disk cache for the Cognito refresh token so a
# ABOUTME: fresh process can skip SRP+MFA. JSON at 0600; opt-in, never enabled by default.
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol


class TokenCache(Protocol):
    """Persistence seam for auth tokens.

    Implementations must tolerate a missing or corrupt store — ``load`` returns ``None``
    rather than raising — and treat ``save`` as best-effort. The value is a small JSON
    object; today that is ``{"username", "refresh_token"}``.
    """

    def load(self) -> dict[str, Any] | None: ...
    def save(self, data: dict[str, Any]) -> None: ...
    def clear(self) -> None: ...


class FileTokenCache:
    """TokenCache backed by a single JSON file, written owner-only (0600).

    The file holds a bearer secret (the refresh token), so it is created with mode 0600 —
    never group/world-readable — and by default lives outside the repo (see ``default``).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path: Path = Path(path)

    @classmethod
    def default(cls) -> FileTokenCache:
        """Cache under ``$XDG_CACHE_HOME`` (or ``~/.cache``), namespaced to this SDK."""
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        return cls(Path(base) / "place-integration-api" / "token.json")

    def load(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text()
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create owner-only from the outset so the secret is never briefly world-readable.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        # O_CREAT only sets the mode on creation; force 0600 if the file pre-existed looser.
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
