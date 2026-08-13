#!/usr/bin/env python3
# ABOUTME: Verifies a release tag matches the version declared in pyproject.toml.
# ABOUTME: Fails cleanly before CI builds or publishes a mislabeled package.
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


def version_for_tag(tag: str, pyproject: Path) -> str:
    """Return the project version when it matches the supplied release tag."""
    with pyproject.open("rb") as file:
        project_version = tomllib.load(file)["project"]["version"]

    tag_version = tag.removeprefix("v")
    if tag_version != project_version:
        raise ValueError(f"tag {tag} does not match project version {project_version}")
    return project_version


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a release tag matches the project version."
    )
    parser.add_argument(
        "--tag", required=True, help="Release tag, with an optional v prefix"
    )
    args = parser.parse_args()

    try:
        version = version_for_tag(
            args.tag, Path(__file__).parents[1] / "pyproject.toml"
        )
    except ValueError as exc:
        parser.exit(1, f"error: {exc}\n")

    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
