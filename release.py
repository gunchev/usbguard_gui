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

TOP = pathlib.Path(__file__).parent


def _fail(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(TOP), *args], text=True)


def main() -> None:
    if len(sys.argv) != 2:
        _fail(f"Usage: python3 {sys.argv[0]} X.Y.Z")

    v = sys.argv[1]

    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        _fail(f"'{v}' is not a valid version (expected X.Y.Z)")

    if subprocess.run(
        ["git", "-C", str(TOP), "diff", "--quiet", "HEAD"], check=False
    ).returncode != 0:
        _fail("uncommitted changes — commit or stash first")

    if f"v{v}" in _git("tag").splitlines():
        _fail(f"tag v{v} already exists")

    # Read package name from pyproject.toml
    pyproject = TOP / "pyproject.toml"
    m = re.search(r'^name\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    if not m:
        _fail("cannot read name from pyproject.toml")
    name = m.group(1)

    # Bump version strings
    print(f"▶ Bumping version to {v} ...")
    for path, pattern, replacement in [
        (pyproject,                   r'^version = ".*"',       f'version = "{v}"'),
        (TOP / "src" / name / "__init__.py",
                                      r'^__version__ = ".*"', f'__version__ = "{v}"'),
    ]:
        text = path.read_text()
        path.write_text(re.sub(pattern, replacement, text, flags=re.MULTILINE))

    # Build CHANGELOG section from commits since the last tag
    print("▶ Updating CHANGELOG.md ...")
    all_tags = [t for t in _git("tag", "--sort=-version:refname").strip().splitlines() if t]
    last_tag = all_tags[0] if all_tags else None
    log_range = [f"{last_tag}..HEAD"] if last_tag else []
    commits = _git("log", *log_range, "--oneline", "--no-decorate", "--no-merges").strip()

    since = last_tag or "beginning"
    section = f"## {v} \u2014 {datetime.date.today().isoformat()}\n\n"
    section += f"### Changes since {since}\n\n"
    if commits:
        section += "\n".join(f"- {line}" for line in commits.splitlines()) + "\n"
    section += "\n"

    cl = TOP / "CHANGELOG.md"
    cl.write_text(section + cl.read_text())
    n = len(commits.splitlines()) if commits else 0
    print(f"Updated CHANGELOG.md ({n} commit{'s' if n != 1 else ''})")

    # Commit and tag
    print(f"▶ Committing and tagging v{v} ...")
    subprocess.run(
        ["git", "-C", str(TOP), "add",
         "pyproject.toml", f"src/{name}/__init__.py", "CHANGELOG.md"],
        check=True,
    )
    subprocess.run(["git", "-C", str(TOP), "commit", "-m", f"Release {v}"], check=True)
    subprocess.run(
        ["git", "-C", str(TOP), "tag", "-a", f"v{v}", "-m", f"Version {v}"],
        check=True,
    )

    # Build the distribution while the tree is at the release version
    print("▶ Building distribution ...")
    subprocess.run(["python3", "-m", "build"], check=True, cwd=str(TOP))

    # Bump to next dev version so the repo never sits on a release version
    major, minor, patch = (int(x) for x in v.split("."))
    dev_v = f"{major}.{minor}.{patch + 1}-dev"
    print(f"▶ Bumping to {dev_v} ...")
    init = TOP / "src" / name / "__init__.py"
    init.write_text(re.sub(r'^__version__ = ".*"', f'__version__ = "{dev_v}"',
                           init.read_text(), flags=re.MULTILINE))
    subprocess.run(
        ["git", "-C", str(TOP), "add", f"src/{name}/__init__.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(TOP), "commit", "-m", f"Start {dev_v}"],
        check=True,
    )

    print(f"\n✓ Released v{v}, repo now at {dev_v}.  Push with:")
    print("      git push && git push --tags")


if __name__ == "__main__":
    main()
