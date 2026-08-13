# ABOUTME: Verifies release tags match the version declared by the SDK package.
# ABOUTME: Guards the release workflow from publishing a mislabeled artifact.
from pathlib import Path

import pytest

from scripts.check_release import version_for_tag


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
