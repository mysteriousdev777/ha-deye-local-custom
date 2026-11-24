"""The installed packages are the reviewed wheels; the fingerprint notices extra files."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

from custom_components.deye_local_custom import packages

# The wheels reviewed on 2026-11-24, each the same as its git tag (pysolarmanv5 v3.0.6,
# umodbus 1.0.4, pyserial v3.5). CI installs them from PyPI by these versions: a
# different file served for the same version fails here. The home's health check keeps
# the same values for the copies Home Assistant installs; change both together, after
# reviewing the new wheels.
REVIEWED = {
    "pysolarmanv5": {
        "version": "3.0.6",
        "files": 3,
        "sha256": "14e73cb5a037c1902a7cc635a6ff83d8ca9ce92f8fda95da47bc97c67b1946a6",
        "other": [],
    },
    "umodbus": {
        "version": "1.0.4",
        "files": 15,
        "sha256": "b567311663497f0828058e9fc610afadda65d8a4223c627ace1aeb566adfb244",
        "other": [],
    },
    "pyserial": {
        "version": "3.5",
        "files": 28,
        "sha256": "cd2f9c92611a1f2c02af84ecb38d64b424bb303827c161744a88ab8cebc4e41f",
        "other": ["pyserial-miniterm", "pyserial-ports"],
    },
}


def test_installed_packages_are_the_reviewed_wheels() -> None:
    assert packages.fingerprints() == REVIEWED


def test_manifest_pins_the_reviewed_versions() -> None:
    manifest = Path("custom_components/deye_local_custom/manifest.json").read_text()
    for name in ("pysolarmanv5", "umodbus"):
        assert f'"{name}=={REVIEWED[name]["version"]}"' in manifest


def test_extra_and_changed_files_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted .pth is listed by name; a changed module changes the digest."""
    (tmp_path / "pkg").mkdir()
    module = tmp_path / "pkg" / "__init__.py"
    module.write_text("x = 1\n")
    (tmp_path / "evil.pth").write_text("import os\n")
    info = tmp_path / "pkg-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n")
    (info / "top_level.txt").write_text("pkg\n")
    (info / "RECORD").write_text(
        "pkg/__init__.py,,\nevil.pth,,\npkg-1.0.dist-info/RECORD,,\n"
    )
    dist = metadata.PathDistribution(info)
    monkeypatch.setattr(packages.metadata, "distribution", lambda name: dist)

    before = packages.fingerprint("pkg")
    assert (before["version"], before["files"], before["other"]) == (
        "1.0",
        1,
        ["evil.pth"],
    )
    module.write_text("x = 2\n")
    assert packages.fingerprint("pkg")["sha256"] != before["sha256"]


def test_missing_package() -> None:
    assert packages.fingerprint("no-such-package-here") == {"version": None}
