#!/usr/bin/env python3
# ABOUTME: Proves PyPI serves the exact local wheel and sdist by filename and SHA-256.
# ABOUTME: Retries boundedly for index propagation and fails closed on any metadata mismatch.
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


class ReleaseVerificationError(RuntimeError):
    """PyPI does not expose the exact release artifacts built by this workflow."""


FetchJson = Callable[[str], object]
Sleep = Callable[[float], None]
MAX_ATTEMPTS = 10
MAX_RETRY_DELAY = 60.0


def _local_artifact_hashes(dist_dir: Path) -> dict[str, str]:
    if not dist_dir.is_dir():
        raise ReleaseVerificationError(f"dist directory does not exist: {dist_dir}")

    artifacts: dict[str, str] = {}
    for path in sorted(dist_dir.iterdir()):
        if path.name == ".gitignore":
            continue
        if not path.is_file() or not path.name.endswith((".whl", ".tar.gz")):
            raise ReleaseVerificationError(f"unexpected file in dist: {path.name}")
        with path.open("rb") as artifact_file:
            artifacts[path.name] = hashlib.file_digest(
                artifact_file, "sha256"
            ).hexdigest()
    if not artifacts:
        raise ReleaseVerificationError("no release artifacts in dist directory")
    wheel_count = sum(filename.endswith(".whl") for filename in artifacts)
    sdist_count = sum(filename.endswith(".tar.gz") for filename in artifacts)
    if wheel_count != 1 or sdist_count != 1 or len(artifacts) != 2:
        raise ReleaseVerificationError(
            "dist must contain exactly one wheel and one source distribution"
        )
    return artifacts


def _remote_artifact_hashes(
    metadata: object, project: str, version: str
) -> dict[str, str]:
    if not isinstance(metadata, Mapping):
        raise ReleaseVerificationError("PyPI returned malformed release metadata")

    info = metadata.get("info")
    urls = metadata.get("urls")
    if not isinstance(info, Mapping) or not isinstance(urls, list):
        raise ReleaseVerificationError("PyPI returned malformed release metadata")
    if info.get("name") != project:
        raise ReleaseVerificationError("PyPI metadata project does not match request")
    if info.get("version") != version:
        raise ReleaseVerificationError("PyPI metadata version does not match request")

    artifacts: dict[str, str] = {}
    for artifact in urls:
        if not isinstance(artifact, Mapping):
            raise ReleaseVerificationError("PyPI returned malformed artifact metadata")
        filename = artifact.get("filename")
        digests = artifact.get("digests")
        if not isinstance(filename, str) or not isinstance(digests, Mapping):
            raise ReleaseVerificationError("PyPI returned malformed artifact metadata")
        digest = digests.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise ReleaseVerificationError(
                "PyPI metadata contains malformed artifact digest"
            )
        if filename in artifacts:
            raise ReleaseVerificationError(
                f"PyPI metadata contains duplicate filename: {filename}"
            )
        artifacts[filename] = digest.lower()
    return artifacts


def _verify_artifact_sets(local: Mapping[str, str], remote: Mapping[str, str]) -> None:
    missing = sorted(local.keys() - remote.keys())
    if missing:
        raise ReleaseVerificationError(
            f"PyPI release is missing local artifacts: {', '.join(missing)}"
        )
    unexpected = sorted(remote.keys() - local.keys())
    if unexpected:
        raise ReleaseVerificationError(
            f"PyPI release contains unexpected artifacts: {', '.join(unexpected)}"
        )
    mismatched = sorted(
        filename for filename, digest in local.items() if remote[filename] != digest
    )
    if mismatched:
        raise ReleaseVerificationError(
            "PyPI artifact digest does not match local file: " + ", ".join(mismatched)
        )


def pypi_request(url: str) -> Request:
    """Build a request that does not reuse stale release metadata."""
    return Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "place-integration-api-release-verifier/0.3.0",
        },
    )


def fetch_pypi_json(url: str, *, timeout: float = 10.0) -> object:
    """Read public PyPI JSON metadata without credentials."""
    request = pypi_request(url)
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def verify_pypi_release(
    project: str,
    version: str,
    dist_dir: Path,
    *,
    fetch_json: FetchJson = fetch_pypi_json,
    sleep: Sleep = time.sleep,
    attempts: int = 6,
    retry_delay: float = 5.0,
) -> dict[str, str]:
    """Verify PyPI exposes exactly the local artifact names and SHA-256 digests."""
    if not 1 <= attempts <= MAX_ATTEMPTS:
        raise ValueError(f"attempts must be between 1 and {MAX_ATTEMPTS}")
    if not 0 <= retry_delay <= MAX_RETRY_DELAY:
        raise ValueError(
            f"retry_delay must be between 0 and {MAX_RETRY_DELAY:g} seconds"
        )

    local = _local_artifact_hashes(dist_dir)
    url = (
        "https://pypi.org/pypi/"
        f"{quote(project, safe='')}/{quote(version, safe='')}/json"
    )
    last_error: ReleaseVerificationError | None = None
    for attempt in range(attempts):
        try:
            metadata = fetch_json(url)
            remote = _remote_artifact_hashes(metadata, project, version)
            _verify_artifact_sets(local, remote)
            return local
        except ReleaseVerificationError as exc:
            last_error = exc
        except Exception:
            last_error = ReleaseVerificationError(
                "unable to read PyPI release metadata"
            )
        if attempt + 1 < attempts:
            sleep(retry_delay)

    if last_error is None:  # defensive: validated attempts always execute the loop
        raise ReleaseVerificationError("PyPI release verification did not run")
    raise last_error from None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify PyPI serves the exact local release artifacts."
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist-dir", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    try:
        verified = verify_pypi_release(
            args.project,
            args.version,
            args.dist_dir,
            fetch_json=partial(fetch_pypi_json, timeout=args.timeout),
            attempts=args.attempts,
            retry_delay=args.retry_delay,
        )
    except (ReleaseVerificationError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")

    print(f"verified {len(verified)} PyPI artifacts for {args.project} {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
