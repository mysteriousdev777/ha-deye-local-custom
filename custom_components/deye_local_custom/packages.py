"""Fingerprints of the installed Python packages the integration runs on.

Home Assistant installs the requirements by exact version but without hashes, and again
after each Core update (they live in the Core container). Diagnostics report a version
and one digest over the files each wheel installed, so a check outside Home Assistant
can compare them with the reviewed wheels (the tests do it too, against what CI
installs).
"""

from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path
from typing import Any

# The requirements of manifest.json, and umodbus's own requirement: pyserial (~=3.4, not
# pinned; only umodbus.server.serial imports it, which nothing here loads).
PACKAGES = ("pysolarmanv5", "umodbus", "pyserial")


def fingerprint(name: str) -> dict[str, Any]:
    """`version`; `sha256` over the sorted "path sha256" lines of the files in the
    package's own directories (top_level.txt), bytecode left out; `other`: the names of
    any other files it installed besides its metadata - scripts (their first line names
    the interpreter, so no hash), and anything that should not be there, like a .pth.
    """
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return {"version": None}
    top = set((dist.read_text("top_level.txt") or "").split())
    lines: list[str] = []
    other: list[str] = []
    for path in dist.files or ():
        parts = path.parts
        if parts[0].endswith(".dist-info") or "__pycache__" in parts:
            continue
        if len(parts) > 1 and parts[0] in top and path.suffix != ".pyc":
            try:
                digest = hashlib.sha256(
                    Path(str(path.locate())).read_bytes()
                ).hexdigest()
            except OSError:
                digest = "missing"
            lines.append(f"{path.as_posix()} {digest}")
        else:
            other.append(path.name)
    return {
        "version": dist.version,
        "files": len(lines),
        "sha256": hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest(),
        "other": sorted(other),
    }


def fingerprints() -> dict[str, dict[str, Any]]:
    """Of every package in PACKAGES. Reads files: not in the event loop."""
    return {name: fingerprint(name) for name in PACKAGES}
