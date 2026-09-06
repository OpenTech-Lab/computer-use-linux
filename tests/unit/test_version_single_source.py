"""The package version must have exactly one source of truth.

There were three copies of it once: pyproject.toml, __init__.__version__, and a hardcoded
literal in the CLI's --version. A release script can only reasonably update one, so the
others silently went stale -- `cul --version` would keep reporting the previous release
forever. These tests lock the arrangement that fixed it.
"""

from __future__ import annotations

import re
from importlib.metadata import version as installed_version
from pathlib import Path

import computer_use_linux

ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_reads_the_version_from_the_package() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {attr = "computer_use_linux.__version__"}' in pyproject
    # A literal version in [project] would shadow the dynamic one and drift.
    project_block = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]
    assert not re.search(r'^version = "', project_block, re.MULTILINE)


def test_installed_metadata_matches_the_package_attribute() -> None:
    assert installed_version("computer-use-linux") == computer_use_linux.__version__


def test_cli_version_is_not_hardcoded() -> None:
    cli = (ROOT / "src/computer_use_linux/cli.py").read_text()
    assert "--version" in cli
    assert not re.search(r'version="computer-use-linux \d+\.\d+\.\d+"', cli), (
        "the CLI --version string is hardcoded again; derive it from __version__"
    )
