# ABOUTME: Tests for FileTokenCache — JSON round-trip, graceful misses/corruption,
# ABOUTME: owner-only (0600) file perms, XDG default path, and clear(), all in tmp_path.
from __future__ import annotations

import stat
from pathlib import Path

from place.auth.token_cache import FileTokenCache


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    cache = FileTokenCache(tmp_path / "token.json")
    cache.save({"username": "alice", "refresh_token": "rt-1"})
    assert cache.load() == {"username": "alice", "refresh_token": "rt-1"}


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert FileTokenCache(tmp_path / "absent.json").load() is None


def test_load_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("{not valid json")
    assert FileTokenCache(path).load() is None


def test_load_returns_none_on_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("[1, 2, 3]")
    assert FileTokenCache(path).load() is None


def test_saved_file_is_owner_read_write_only(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    FileTokenCache(path).save({"refresh_token": "rt-1"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_tightens_permissions_on_a_preexisting_loose_file(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("{}")
    path.chmod(0o644)
    FileTokenCache(path).save({"refresh_token": "rt-1"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_creates_missing_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "token.json"
    FileTokenCache(path).save({"refresh_token": "rt-1"})
    assert path.exists()


def test_clear_removes_file_and_is_idempotent(tmp_path: Path) -> None:
    cache = FileTokenCache(tmp_path / "token.json")
    cache.save({"refresh_token": "rt-1"})
    cache.clear()
    assert cache.load() is None
    cache.clear()  # clearing an already-absent file must not raise


def test_default_path_respects_xdg_cache_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cache = FileTokenCache.default()
    assert cache.path == tmp_path / "place-integration-api" / "token.json"
    cache.save({"refresh_token": "rt-1"})
    assert cache.load() == {"refresh_token": "rt-1"}
