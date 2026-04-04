"""Tests for the release helper script."""

import re
import subprocess
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def load_release_module():
    """Load the release module from the project root."""
    release_path = PROJECT_ROOT / "release.py"
    loader = SourceFileLoader("release", str(release_path))
    module = types.ModuleType("release")
    module.__file__ = str(release_path)
    loader.exec_module(module)
    return module


release = load_release_module()


class TestVersionValidation:
    """Test version string validation."""

    def test_valid_version(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", "1.2.3") is not None
        assert re.fullmatch(r"\d+\.\d+\.\d+", "0.0.1") is not None
        assert re.fullmatch(r"\d+\.\d+\.\d+", "10.20.30") is not None

    def test_invalid_version_no_dots(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", "123") is None

    def test_invalid_version_with_prefix(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", "v1.2.3") is None

    def test_invalid_version_with_suffix(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", "1.2.3-dev") is None

    def test_invalid_version_missing_parts(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", "1.2") is None
        assert re.fullmatch(r"\d+\.\d+\.\d+", "1.2.3.4") is None


class TestBumpVersion:
    """Test bump_version function."""

    INIT_TEMPLATE = '''"""usbguard_gui — KDE/Qt system tray GUI for USBGuard."""

__version__ = "{old_version}"

__author__ = "Doncho Nikolaev Gunchev"
__license__ = "GPL-2.0-or-later"
'''

    def test_bump_version(self, tmp_path):
        init_file = tmp_path / "__init__.py"
        init_file.write_text(self.INIT_TEMPLATE.format(old_version="0.0.7"))
        release.bump_version(init_file, "0.0.8")

        content = init_file.read_text()
        assert '__version__ = "0.0.8"' in content
        assert '__version__ = "0.0.7"' not in content

    def test_bump_version_preserves_other_content(self, tmp_path):
        init_file = tmp_path / "__init__.py"
        init_file.write_text(self.INIT_TEMPLATE.format(old_version="1.2.3"))
        release.bump_version(init_file, "1.2.4")

        content = init_file.read_text()
        assert "__author__" in content
        assert "__license__" in content
        assert "Doncho Nikolaev Gunchev" in content


class TestGetLastTag:
    """Test get_last_tag function."""

    def test_returns_most_recent_tag(self):
        original = release._git
        release._git = MagicMock(return_value="v1.0.0\nv0.9.0\nv0.8.0")
        try:
            result = release.get_last_tag()
            assert result == "v1.0.0"
        finally:
            release._git = original

    def test_returns_none_when_no_tags(self):
        original = release._git
        release._git = MagicMock(return_value="")
        try:
            result = release.get_last_tag()
            assert result is None
        finally:
            release._git = original


class TestBuildChangelog:
    """Test build_changelog function."""

    def test_changelog_format(self):
        original = release._git
        release._git = MagicMock(return_value="feat: add feature\nfix: bug fix")
        try:
            section = release.build_changelog("0.0.8", [], "v0.0.7")
            assert "## 0.0.8" in section
            assert "### Changes since v0.0.7" in section
            assert "- feat: add feature" in section
            assert "- fix: bug fix" in section
        finally:
            release._git = original

    def test_changelog_empty_commits(self):
        original = release._git
        release._git = MagicMock(return_value="")
        try:
            section = release.build_changelog("0.0.8", [], None)
            assert "## 0.0.8" in section
            assert "- " not in section
        finally:
            release._git = original


class TestPackageNameExtraction:
    """Test reading package name from pyproject.toml."""

    def test_extract_name(self):
        content = '[project]\nname = "usbguard_gui"\ndynamic = ["version"]\n'
        m = re.search(r'^name\s*=\s*"([^"]+)"', content, re.MULTILINE)
        assert m is not None
        assert m.group(1) == "usbguard_gui"

    def test_missing_name(self):
        content = "[project]\ndynamic = ['version']"
        m = re.search(r'^name\s*=\s*"([^"]+)"', content, re.MULTILINE)
        assert m is None


class TestRelease:
    """Test the release() function with mocked git and filesystem."""

    INIT_CONTENT = '''"""usbguard_gui — KDE/Qt system tray GUI for USBGuard."""

__version__ = "0.0.7"

__author__ = "Doncho Nikolaev Gunchev"
__license__ = "GPL-2.0-or-later"
'''

    CHANGELOG_CONTENT = "# Changelog\n"
    PYPROJECT_CONTENT = '[project]\nname = "usbguard_gui"\n'

    @pytest.fixture()
    def mock_env(self, tmp_path):
        src_dir = tmp_path / "src" / "usbguard_gui"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text(self.INIT_CONTENT)
        (tmp_path / "CHANGELOG.md").write_text(self.CHANGELOG_CONTENT)
        (tmp_path / "pyproject.toml").write_text(self.PYPROJECT_CONTENT)

        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        return tmp_path

    def test_fails_with_invalid_version(self, mock_env, monkeypatch, capsys):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(SystemExit):
            release.release("invalid", top=mock_env)
        assert "is not a valid version" in capsys.readouterr().err

    def test_fails_with_dev_version(self, mock_env, monkeypatch, capsys):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr(subprocess, "run", mock_run)
        with pytest.raises(SystemExit):
            release.release("1.0.0-dev", top=mock_env)
        assert "is not a valid version" in capsys.readouterr().err

    def test_fails_with_uncommitted_changes(self, mock_env, capsys):
        mock_run = MagicMock(return_value=MagicMock(returncode=1))
        original_run = release.release.__globals__["subprocess"].run
        release.release.__globals__["subprocess"].run = mock_run
        try:
            with pytest.raises(SystemExit):
                release.release("1.0.0", top=mock_env, git=MagicMock(return_value=""))
            assert "uncommitted changes" in capsys.readouterr().err
        finally:
            release.release.__globals__["subprocess"].run = original_run

    def test_fails_with_existing_tag(self, mock_env, capsys):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        original_run = release.release.__globals__["subprocess"].run
        release.release.__globals__["subprocess"].run = mock_run

        original_git = release._git
        release._git = lambda *args: "v1.0.0\nv0.9.0"

        original_get_last_tag = release.get_last_tag
        release.get_last_tag = lambda: "v0.9.0"

        try:
            with pytest.raises(SystemExit):
                release.release("1.0.0", top=mock_env, git=MagicMock(return_value="v1.0.0"))
            assert "tag v1.0.0 already exists" in capsys.readouterr().err
        finally:
            release.release.__globals__["subprocess"].run = original_run
            release._git = original_git
            release.get_last_tag = original_get_last_tag

    def test_fails_with_missing_pyproject_name(self, mock_env, capsys):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        original_run = release.release.__globals__["subprocess"].run
        release.release.__globals__["subprocess"].run = mock_run
        try:
            (mock_env / "pyproject.toml").write_text("[project]\n")
            with pytest.raises(SystemExit):
                release.release("1.0.0", top=mock_env, git=MagicMock(return_value=""))
            assert "cannot read name from pyproject.toml" in capsys.readouterr().err
        finally:
            release.release.__globals__["subprocess"].run = original_run

    def test_bumps_version_and_updates_changelog(self, mock_env):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        original_run = release.release.__globals__["subprocess"].run
        release.release.__globals__["subprocess"].run = mock_run
        try:
            release.release(
                "1.0.0",
                top=mock_env,
                git_run=MagicMock(),
                git=MagicMock(return_value=""),
                build_impl=MagicMock(),
            )
        finally:
            release.release.__globals__["subprocess"].run = original_run

        init_file = mock_env / "src" / "usbguard_gui" / "__init__.py"
        assert '__version__ = "1.0.0"' in init_file.read_text()

        changelog = (mock_env / "CHANGELOG.md").read_text()
        assert "## 1.0.0" in changelog

    def test_commits_and_tags(self, mock_env):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        original_run = release.release.__globals__["subprocess"].run
        release.release.__globals__["subprocess"].run = mock_run
        try:
            mock_git_run = MagicMock()
            release.release(
                "1.0.0",
                top=mock_env,
                git_run=mock_git_run,
                git=MagicMock(return_value=""),
                build_impl=MagicMock(),
            )

            calls = [str(c) for c in mock_git_run.call_args_list]
            assert any("commit" in c and "Release 1.0.0" in c for c in calls)
            assert any("tag" in c and "v1.0.0" in c for c in calls)
        finally:
            release.release.__globals__["subprocess"].run = original_run

    def test_runs_build(self, mock_env):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        original_run = release.release.__globals__["subprocess"].run
        release.release.__globals__["subprocess"].run = mock_run
        try:
            mock_build = MagicMock()
            release.release(
                "1.0.0",
                top=mock_env,
                git_run=MagicMock(),
                git=MagicMock(return_value=""),
                build_impl=mock_build,
            )

            mock_build.assert_called_once()
        finally:
            release.release.__globals__["subprocess"].run = original_run

    def test_includes_commits_in_changelog(self, mock_env):
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        original_run = release.release.__globals__["subprocess"].run
        release.release.__globals__["subprocess"].run = mock_run

        original_git = release._git
        release._git = lambda *args: "v0.9.0" if "tag" in args else "feat: add feature\nfix: bug fix"

        original_get_last_tag = release.get_last_tag
        release.get_last_tag = lambda: "v0.9.0"

        try:
            release.release(
                "1.0.0",
                top=mock_env,
                git_run=MagicMock(),
                git=MagicMock(return_value=""),
                build_impl=MagicMock(),
            )

            changelog = (mock_env / "CHANGELOG.md").read_text()
            assert "- feat: add feature" in changelog
            assert "- fix: bug fix" in changelog
            assert "v0.9.0" in changelog
        finally:
            release.release.__globals__["subprocess"].run = original_run
            release._git = original_git
            release.get_last_tag = original_get_last_tag


class TestMainFlow:
    """Test the main() function."""

    def test_fails_without_version_arg(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        release_module = load_release_module()
        monkeypatch.setattr(sys, "argv", ["release.py"])

        with pytest.raises(SystemExit):
            release_module.main()

    def test_fails_with_invalid_version(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        release_module = load_release_module()
        monkeypatch.setattr(sys, "argv", ["release.py", "invalid"])

        with pytest.raises(SystemExit):
            release_module.main()
