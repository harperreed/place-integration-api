# ABOUTME: Verifies release tags match the version declared by the SDK package.
# ABOUTME: Guards the release workflow from publishing a mislabeled artifact.
import hashlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

from scripts.check_release import version_for_tag
from scripts.verify_pypi_release import (
    ReleaseVerificationError,
    pypi_request,
    verify_pypi_release,
)


@pytest.fixture
def pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nversion = "0.3.0"\n')
    return path


@pytest.mark.parametrize("tag", ["v0.3.0", "0.3.0"])
def test_version_for_tag_accepts_matching_version(tag: str, pyproject: Path) -> None:
    assert version_for_tag(tag, pyproject) == "0.3.0"


def test_version_for_tag_rejects_mismatched_version(pyproject: Path) -> None:
    with pytest.raises(
        ValueError,
        match=r"^tag v0\.3\.1 does not match project version 0\.3\.0$",
    ):
        version_for_tag("v0.3.1", pyproject)


def _write_artifacts(dist_dir: Path) -> dict[str, str]:
    artifacts = {
        "place_integration_api-0.3.0-py3-none-any.whl": b"wheel",
        "place_integration_api-0.3.0.tar.gz": b"sdist",
    }
    dist_dir.mkdir()
    for filename, content in artifacts.items():
        (dist_dir / filename).write_bytes(content)
    return {
        filename: hashlib.sha256(content).hexdigest()
        for filename, content in artifacts.items()
    }


def _metadata(artifacts: dict[str, str]) -> dict[str, object]:
    return {
        "info": {"name": "place-integration-api", "version": "0.3.0"},
        "urls": [
            {"filename": filename, "digests": {"sha256": digest}}
            for filename, digest in artifacts.items()
        ],
    }


class SequenceFetcher:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, url: str) -> object:
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_pypi_request_bypasses_stale_release_metadata_cache() -> None:
    request = pypi_request("https://pypi.org/pypi/place-integration-api/0.3.0/json")

    headers = dict(request.header_items())
    assert headers["Accept"] == "application/json"
    assert headers["Cache-control"] == "no-cache"


def test_verify_pypi_release_accepts_exact_filename_and_digest_set(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    artifacts = _write_artifacts(dist_dir)
    fetcher = SequenceFetcher([_metadata(artifacts)])

    verified = verify_pypi_release(
        "place-integration-api",
        "0.3.0",
        dist_dir,
        fetch_json=fetcher,
        sleep=lambda _: None,
        attempts=1,
    )

    assert verified == artifacts
    assert fetcher.urls == ["https://pypi.org/pypi/place-integration-api/0.3.0/json"]


def test_verify_pypi_release_retries_until_all_artifacts_propagate(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    artifacts = _write_artifacts(dist_dir)
    partial = _metadata(dict(list(artifacts.items())[:1]))
    fetcher = SequenceFetcher([partial, _metadata(artifacts)])
    sleeps: list[float] = []

    verify_pypi_release(
        "place-integration-api",
        "0.3.0",
        dist_dir,
        fetch_json=fetcher,
        sleep=sleeps.append,
        attempts=2,
        retry_delay=0.25,
    )

    assert len(fetcher.urls) == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda metadata: metadata["urls"].pop(),
            "PyPI release is missing local artifacts",
        ),
        (
            lambda metadata: metadata["urls"].append(
                {
                    "filename": "place_integration_api-0.3.0-extra.whl",
                    "digests": {"sha256": "a" * 64},
                }
            ),
            "PyPI release contains unexpected artifacts",
        ),
        (
            lambda metadata: metadata["urls"][0]["digests"].update(
                {"sha256": "b" * 64}
            ),
            "PyPI artifact digest does not match local file",
        ),
        (
            lambda metadata: metadata["urls"].append(metadata["urls"][0].copy()),
            "PyPI metadata contains duplicate filename",
        ),
        (
            lambda metadata: metadata["urls"][0]["digests"].update(
                {"sha256": "not-a-sha256"}
            ),
            "PyPI metadata contains malformed artifact digest",
        ),
    ],
)
def test_verify_pypi_release_rejects_unsafe_remote_artifact_state(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    dist_dir = tmp_path / "dist"
    metadata = _metadata(_write_artifacts(dist_dir))
    mutate(metadata)

    with pytest.raises(ReleaseVerificationError, match=message):
        verify_pypi_release(
            "place-integration-api",
            "0.3.0",
            dist_dir,
            fetch_json=SequenceFetcher([metadata]),
            sleep=lambda _: None,
            attempts=1,
        )


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {},
        {"info": {"name": "wrong-project", "version": "0.3.0"}, "urls": []},
        {
            "info": {"name": "place-integration-api", "version": "0.3.1"},
            "urls": [],
        },
        {
            "info": {"name": "place-integration-api", "version": "0.3.0"},
            "urls": [{}],
        },
    ],
)
def test_verify_pypi_release_rejects_malformed_or_wrong_metadata(
    tmp_path: Path, metadata: object
) -> None:
    dist_dir = tmp_path / "dist"
    _write_artifacts(dist_dir)

    with pytest.raises(ReleaseVerificationError):
        verify_pypi_release(
            "place-integration-api",
            "0.3.0",
            dist_dir,
            fetch_json=SequenceFetcher([metadata]),
            sleep=lambda _: None,
            attempts=1,
        )


def test_verify_pypi_release_rejects_empty_or_dirty_dist_directory(
    tmp_path: Path,
) -> None:
    empty_dist = tmp_path / "empty"
    empty_dist.mkdir()
    with pytest.raises(ReleaseVerificationError, match="no release artifacts"):
        verify_pypi_release(
            "place-integration-api",
            "0.3.0",
            empty_dist,
            fetch_json=SequenceFetcher([]),
            sleep=lambda _: None,
            attempts=1,
        )

    dirty_dist = tmp_path / "dirty"
    _write_artifacts(dirty_dist)
    (dirty_dist / "notes.txt").write_text("not a release artifact")
    with pytest.raises(ReleaseVerificationError, match="unexpected file in dist"):
        verify_pypi_release(
            "place-integration-api",
            "0.3.0",
            dirty_dist,
            fetch_json=SequenceFetcher([]),
            sleep=lambda _: None,
            attempts=1,
        )


def test_verify_pypi_release_requires_one_wheel_and_one_sdist(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "place_integration_api-0.3.0-py3-none-any.whl").write_bytes(b"wheel")

    with pytest.raises(
        ReleaseVerificationError, match="exactly one wheel and one source distribution"
    ):
        verify_pypi_release(
            "place-integration-api",
            "0.3.0",
            dist_dir,
            fetch_json=SequenceFetcher([]),
            sleep=lambda _: None,
            attempts=1,
        )


def test_verify_pypi_release_retries_network_failure_then_fails_cleanly(
    tmp_path: Path,
) -> None:
    dist_dir = tmp_path / "dist"
    _write_artifacts(dist_dir)
    fetcher = SequenceFetcher([OSError("network secret"), OSError("network secret")])
    sleeps: list[float] = []

    with pytest.raises(
        ReleaseVerificationError, match="unable to read PyPI release metadata"
    ) as caught:
        verify_pypi_release(
            "place-integration-api",
            "0.3.0",
            dist_dir,
            fetch_json=fetcher,
            sleep=sleeps.append,
            attempts=2,
            retry_delay=0.5,
        )

    assert "network secret" not in str(caught.value)
    assert len(fetcher.urls) == 2
    assert sleeps == [0.5]


@pytest.mark.parametrize(
    ("attempts", "retry_delay"),
    [(0, 1.0), (11, 1.0), (1, -0.1), (1, 60.1)],
)
def test_verify_pypi_release_rejects_unbounded_retry_settings(
    tmp_path: Path, attempts: int, retry_delay: float
) -> None:
    dist_dir = tmp_path / "dist"
    _write_artifacts(dist_dir)

    with pytest.raises(ValueError):
        verify_pypi_release(
            "place-integration-api",
            "0.3.0",
            dist_dir,
            fetch_json=SequenceFetcher([]),
            sleep=lambda _: None,
            attempts=attempts,
            retry_delay=retry_delay,
        )


def test_release_workflow_pins_locked_python_toolchain_and_verification() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/pypi.yml").read_text()
    with (root / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)

    assert project["tool"]["uv"]["required-version"] == "==0.9.25"
    assert project["build-system"]["requires"] == ["setuptools==83.0.0"]
    assert 'version: "0.9.25"' in workflow
    assert 'python-version: "3.11"' in workflow
    assert "uv run --locked python scripts/check_release.py" in workflow
    for line in workflow.splitlines():
        if "uv run " in line:
            assert "uv run --locked " in line
    assert workflow.count('version: "0.9.25"') == 2
    assert workflow.count('python-version: "3.11"') == 2
    assert "continue-on-error: true" in workflow
    assert "skip-existing: true" in workflow
    assert "scripts/verify_pypi_release.py" in workflow
    assert "needs: verify-release" in workflow
