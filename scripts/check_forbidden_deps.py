#!/usr/bin/env python3
"""Fail if a remote-media-fetch dependency shows up in any manifest. See CLAUDE.md: there is no
fourth ingress lane, and no yt-dlp-class dependency belongs in this project."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

FORBIDDEN = ("yt-dlp", "yt_dlp", "youtube-dl", "youtube_dl", "pytube")

MANIFEST_NAMES_EXACT = ("pyproject.toml", "package.json")
MANIFEST_PREFIX_SUFFIX = ("requirements", ".txt")  # requirements*.txt

LOCKFILE_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock")

PRUNE_DIRS = {"node_modules", ".venv", ".git"}

# Leading package-name token, e.g. "yt-dlp>=1.0" -> "yt-dlp".
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _is_manifest(name: str) -> bool:
    if name in MANIFEST_NAMES_EXACT:
        return True
    prefix, suffix = MANIFEST_PREFIX_SUFFIX
    return name.startswith(prefix) and name.endswith(suffix)


def _is_lockfile(name: str) -> bool:
    return name in LOCKFILE_NAMES


def find_manifests_and_lockfiles(root: Path) -> tuple[list[Path], list[Path]]:
    """Walk the tree once, pruning ignored directories entirely, and bucket matches."""
    manifests: list[Path] = []
    lockfiles: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in PRUNE_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                if _is_manifest(entry.name):
                    manifests.append(entry)
                elif _is_lockfile(entry.name):
                    lockfiles.append(entry)
    return manifests, lockfiles


def _extract_name(requirement: str) -> str | None:
    requirement = requirement.strip()
    match = NAME_RE.match(requirement)
    return match.group(0) if match else None


def _names_from_pyproject(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
        return names

    project = data.get("project", {})
    for requirement in project.get("dependencies", []) or []:
        name = _extract_name(str(requirement))
        if name:
            names.add(name)

    optional = project.get("optional-dependencies", {}) or {}
    for group in optional.values():
        for requirement in group or []:
            name = _extract_name(str(requirement))
            if name:
                names.add(name)

    return names


def _names_from_requirements_txt(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return names

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        name = _extract_name(line)
        if name:
            names.add(name)

    return names


def _names_from_package_json(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
        return names

    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.update(section.keys())

    return names


def manifest_dependency_names(path: Path) -> set[str]:
    name = path.name
    if name == "pyproject.toml":
        return _names_from_pyproject(path)
    if name == "package.json":
        return _names_from_package_json(path)
    if _is_manifest(name):  # requirements*.txt
        return _names_from_requirements_txt(path)
    return set()


def check_manifest(path: Path, root: Path, hits: list[str]) -> None:
    declared = {n.lower() for n in manifest_dependency_names(path)}
    for name in FORBIDDEN:
        if name.lower() in declared:
            hits.append(
                f"{path.relative_to(root)}: declares forbidden dependency '{name}' (manifest)"
            )


def check_lockfile(path: Path, root: Path, hits: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return
    for name in FORBIDDEN:
        if re.search(re.escape(name), text, re.IGNORECASE):
            hits.append(f"{path.relative_to(root)}: found forbidden dependency '{name}' (lockfile)")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    hits: list[str] = []

    manifests, lockfiles = find_manifests_and_lockfiles(root)

    for manifest in manifests:
        check_manifest(manifest, root, hits)

    for lockfile in lockfiles:
        check_lockfile(lockfile, root, hits)

    if hits:
        print("Forbidden dependency check failed:", file=sys.stderr)
        for hit in hits:
            print(f"  - {hit}", file=sys.stderr)
        print(
            "\nSee CLAUDE.md: no yt-dlp/youtube-dl/pytube-class dependency belongs in this repo.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
