"""Refresh lock files from the installed, verified Python environment."""

from importlib.metadata import distribution
from pathlib import Path
import subprocess
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parents[1]
seen = {}


def add(requirement):
    name = canonicalize_name(requirement.name)
    if name in seen:
        return
    dist = distribution(name)
    seen[name] = dist.version
    for raw in dist.requires or []:
        dependency = Requirement(raw)
        if dependency.marker is None or any(
            dependency.marker.evaluate({"extra": extra}) for extra in ["", *requirement.extras]
        ):
            add(dependency)


for line in (root / "requirements.txt").read_text().splitlines():
    if line and not line.startswith("#"):
        add(Requirement(line))
(root / "requirements.lock").write_text(
    "# Runtime dependencies resolved on Python 3.12\n"
    + "\n".join(f"{name}=={version}" for name, version in sorted(seen.items()))
    + "\n",
    encoding="utf-8",
)
frozen = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
(root / "requirements-dev.lock").write_text(
    "# Runtime and development dependencies\n" + frozen, encoding="utf-8"
)
print("Dependency locks refreshed. Verify tests and Linux container before publishing.")
