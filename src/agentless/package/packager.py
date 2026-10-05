"""Collect the files Agent Runtime builds from and fingerprint them; fully offline."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pathspec

# Same defaults and precedence as agents-cli: .gcloudignore, else .gitignore.
_DEFAULT_IGNORES = (".git", ".gcloudignore", ".gitignore", ".agentless/")
_INCLUDE_DIRECTIVE = "#!include:"
# Rewritten on every deploy, so excluded from the fingerprint (still uploaded, like agents-cli does).
_UNHASHED = frozenset({"deployment_metadata.json"})


class PackageError(Exception):
    """The source tree cannot be deployed."""


@dataclass(frozen=True)
class Package:
    """Deterministic file list and content hash of an agent source tree."""

    root: Path
    files: tuple[str, ...]
    sha256: str

    @property
    def source_packages(self) -> list[str]:
        """`./`-prefixed relative paths, the form the Vertex SDK expects."""
        return [f"./{f}" for f in self.files]


def ignore_lines(root: Path) -> list[str]:
    """Gitignore-style patterns for `root`."""
    lines = list(_DEFAULT_IGNORES)
    source = root / ".gcloudignore"
    if not source.exists():
        source = root / ".gitignore"
    if not source.exists():
        return lines
    for raw in source.read_text(encoding="utf-8").splitlines():
        directive = raw.strip()
        if directive.startswith(_INCLUDE_DIRECTIVE):
            included = root / directive.removeprefix(_INCLUDE_DIRECTIVE).strip()
            if included.exists():
                lines += included.read_text(encoding="utf-8").splitlines()
        else:
            lines.append(raw)
    return lines


def collect(root: Path, exclude: tuple[str, ...] = ()) -> Package:
    """List deployable files under `root` and hash paths plus contents.

    Args:
        root: agents-cli project directory.
        exclude: Extra relative paths to drop, e.g. agent.yaml so config edits don't trigger a rebuild.
    """
    spec = pathspec.PathSpec.from_lines("gitignore", [*ignore_lines(root), *(f"/{e}" for e in exclude)])
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not spec.match_file((rel_dir / d).as_posix() + "/"))
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if not spec.match_file(rel):
                files.append(rel)
    if "Dockerfile" not in files:
        raise PackageError("Dockerfile is missing or excluded by .gcloudignore/.gitignore")

    digest = hashlib.sha256()
    for rel in (f for f in files if f not in _UNHASHED):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256((root / rel).read_bytes()).digest())
    return Package(root, tuple(files), digest.hexdigest())


def write_archive(package: Package, dest: Path) -> Path:
    """Write a reproducible .tar.gz of the package (for `agentless package`)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as tar:
        for rel in package.files:
            info = tar.gettarinfo(package.root / rel, arcname=rel)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            with (package.root / rel).open("rb") as fh:
                tar.addfile(info, fh)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(buffer.getvalue())
    return dest
