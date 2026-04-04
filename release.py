#!/usr/bin/env python3
"""Release helper: bump version, update CHANGELOG.md, commit, and tag.

Usage:
    python3 release.py X.Y.Z
"""

from __future__ import annotations

import datetime
import pathlib
import re
import subprocess
import sys
from collections.abc import Callable

TOP = pathlib.Path(__file__).parent


def _fail(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(TOP), *args], text=True)


def _git_run(*args: str) -> None:
    subprocess.run(["git", "-C", str(TOP), *args], check=True)


def bump_version(init_file: pathlib.Path, version: str) -> None:
    """Update __version__ in the package __init__.py file."""
    content = init_file.read_text()
    new_content = re.sub(
        r'^__version__ = ".*"',
        f'__version__ = "{version}"',
        content,
        flags=re.MULTILINE,
    )
    init_file.write_text(new_content)


def build_changelog(version: str, log_range: list[str], since: str | None) -> str:
    """Build the CHANGELOG section string."""
    commits = _git("log", *log_range, "--oneline", "--no-decorate", "--no-merges").strip()
    today = datetime.date.today().isoformat()

    section = f"## {version} \u2014 {today}\n\n"
    section += f"### Changes since {since or 'beginning'}\n\n"
    if commits:
        section += "\n".join(f"- {line}" for line in commits.splitlines()) + "\n"
    section += "\n"
    return section


def get_last_tag() -> str | None:
    """Get the most recent git tag."""
    all_tags = [t for t in _git("tag", "--sort=-version:refname").strip().splitlines() if t]
    return all_tags[0] if all_tags else None


def _default_build_impl(top: pathlib.Path) -> None:
    """Default build implementation."""
    subprocess.run(["python3", "-m", "build"], check=True, cwd=str(top))


def release(
    version: str,
    top: pathlib.Path = TOP,
    git_run: Callable[..., None] = _git_run,
    git: Callable[..., str] = _git,
    build_impl: Callable[[], None] | None = None,
) -> None:
    """Execute the release process for the given version.

    Args:
        version: The version to release (e.g. "1.2.3")
        top: The project root directory
        git_run: Callable for git commands that don't return output
        git: Callable for git commands that return output
        build_impl: Callable to run the build, defaults to subprocess
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        _fail(f"'{version}' is not a valid version (expected X.Y.Z)")

    if subprocess.run(["git", "-C", str(top), "diff", "--quiet", "HEAD"], check=False).returncode != 0:
        _fail("uncommitted changes — commit or stash first")

    if f"v{version}" in git("tag").splitlines():
        _fail(f"tag v{version} already exists")

    pyproject = top / "pyproject.toml"
    m = re.search(r'^name\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    if not m:
        _fail("cannot read name from pyproject.toml")
    name = m.group(1)

    print(f"▶ Bumping version to {version} ...")
    init_file = top / "src" / name / "__init__.py"
    bump_version(init_file, version)

    print("▶ Updating CHANGELOG.md ...")
    last_tag = get_last_tag()
    log_range = [f"{last_tag}..HEAD"] if last_tag else []
    section = build_changelog(version, log_range, last_tag)

    cl = top / "CHANGELOG.md"
    cl.write_text(section + cl.read_text())
    n = len(section.splitlines()) - 4 if last_tag else 0
    print(f"Updated CHANGELOG.md ({n} commit{'s' if n != 1 else ''})")

    print(f"▶ Committing and tagging v{version} ...")
    git_run("add", f"src/{name}/__init__.py", "CHANGELOG.md")
    git_run("commit", "-m", f"Release {version}")
    git_run("tag", "-a", f"v{version}", "-m", f"Version {version}")

    print("▶ Building distribution ...")
    if build_impl is None:
        _default_build_impl(top)
    else:
        build_impl()

    print(f"\n✓ Released v{version}.  Push with:")
    print("      git push && git push --tags")


def main() -> None:
    if len(sys.argv) != 2:
        _fail(f"Usage: python3 {sys.argv[0]} X.Y.Z")

    version = sys.argv[1]
    release(version)
