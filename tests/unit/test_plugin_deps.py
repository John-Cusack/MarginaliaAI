"""Tests for plugin dependency installation and loading checks."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from research_engine.domain.errors import PluginError, PluginLoadError
from research_engine.plugins.installer import PluginInstaller
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.manifest import PluginManifest, PluginPermissions
from research_engine.plugins.registry import PluginRegistry


def _make_manifest(
    *,
    pip: list[str] | None = None,
    setup_commands: list[str] | None = None,
    subprocess_perm: bool = False,
) -> PluginManifest:
    return PluginManifest(
        name="test-plugin",
        version="0.1.0",
        author="Test",
        description="A test plugin",
        requires={
            "pip": pip or [],
            "setup_commands": setup_commands or [],
        },
        permissions=PluginPermissions(subprocess=subprocess_perm),
    )


class TestResolvePipCommand:
    def test_uses_uv_when_available(self):
        with patch("shutil.which", return_value="/usr/bin/uv"):
            cmd = PluginInstaller._resolve_pip_command()
        assert cmd == ["uv", "pip", "install"]

    def test_falls_back_to_pip(self):
        with patch("shutil.which", return_value=None):
            cmd = PluginInstaller._resolve_pip_command()
        assert cmd == [sys.executable, "-m", "pip", "install"]


class TestInstallDependencies:
    def _make_installer(self, tmp_path: Path) -> PluginInstaller:
        return PluginInstaller(tmp_path, AsyncMock())

    def test_no_deps_returns_immediately(self, tmp_path: Path):
        installer = self._make_installer(tmp_path)
        manifest = _make_manifest()
        # Should not raise, should not call subprocess
        with patch("subprocess.run") as mock_run:
            installer._install_dependencies(manifest)
            mock_run.assert_not_called()

    def test_pip_install_success(self, tmp_path: Path):
        installer = self._make_installer(tmp_path)
        manifest = _make_manifest(pip=["requests>=2.28", "httpx"])

        mock_proc = MagicMock(returncode=0, stderr="", stdout="")
        with (
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch("shutil.which", return_value="/usr/bin/uv"),
        ):
            installer._install_dependencies(manifest)

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["uv", "pip", "install", "requests>=2.28", "httpx"]

    def test_pip_install_failure_raises(self, tmp_path: Path):
        installer = self._make_installer(tmp_path)
        manifest = _make_manifest(pip=["nonexistent-pkg"])

        mock_proc = MagicMock(returncode=1, stderr="No matching distribution", stdout="")
        with (
            patch("subprocess.run", return_value=mock_proc),
            patch("shutil.which", return_value=None),
        ):
            with pytest.raises(PluginError, match="pip install failed"):
                installer._install_dependencies(manifest)

    def test_setup_commands_without_permission_raises(self, tmp_path: Path):
        installer = self._make_installer(tmp_path)
        manifest = _make_manifest(
            setup_commands=["playwright install chromium"],
            subprocess_perm=False,
        )

        with pytest.raises(PluginError, match="permissions.subprocess is false"):
            installer._install_dependencies(manifest)

    def test_setup_commands_with_permission(self, tmp_path: Path):
        installer = self._make_installer(tmp_path)
        manifest = _make_manifest(
            setup_commands=["playwright install chromium"],
            subprocess_perm=True,
        )

        mock_proc = MagicMock(returncode=0, stderr="", stdout="")
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            installer._install_dependencies(manifest)

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == "playwright install chromium"
        assert call_args[1]["shell"] is True

    def test_setup_command_failure_raises(self, tmp_path: Path):
        installer = self._make_installer(tmp_path)
        manifest = _make_manifest(
            setup_commands=["false"],
            subprocess_perm=True,
        )

        mock_proc = MagicMock(returncode=1, stderr="command failed", stdout="")
        with patch("subprocess.run", return_value=mock_proc):
            with pytest.raises(PluginError, match="Setup command failed"):
                installer._install_dependencies(manifest)

    def test_callback_receives_messages(self, tmp_path: Path):
        installer = self._make_installer(tmp_path)
        manifest = _make_manifest(
            pip=["requests"],
            setup_commands=["echo hello"],
            subprocess_perm=True,
        )

        messages: list[str] = []
        mock_proc = MagicMock(returncode=0, stderr="", stdout="")
        with (
            patch("subprocess.run", return_value=mock_proc),
            patch("shutil.which", return_value=None),
        ):
            installer._install_dependencies(manifest, callback=messages.append)

        assert len(messages) == 2
        assert "pip dependencies" in messages[0]
        assert "Running setup" in messages[1]


class TestInstallCleanupOnDepFailure:
    @pytest.mark.asyncio
    async def test_final_dir_removed_on_dep_failure(self, tmp_path: Path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        installer = PluginInstaller(plugins_dir, AsyncMock())

        # Create a fake repo with a valid manifest
        repo_dir = plugins_dir / "_installing" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "pack.yaml").write_text(
            "name: failing\nversion: 0.1.0\nauthor: Test\n"
            "description: test\nrequires:\n  pip:\n    - bad-package\n"
        )

        def mock_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "git":
                if "clone" in cmd:
                    return MagicMock(returncode=0, stderr="", stdout="")
                if "rev-parse" in cmd:
                    return MagicMock(returncode=0, stderr="", stdout="abc123\n")
            # pip install fails
            return MagicMock(returncode=1, stderr="No matching distribution", stdout="")

        # We also need rename to work — but the repo_dir already exists
        # so we simulate it by pre-creating the final dir after clone
        original_rename = Path.rename

        def patched_rename(self_path: Path, target: Path) -> Path:
            target.mkdir(parents=True, exist_ok=True)
            # Copy pack.yaml
            import shutil
            for item in self_path.iterdir():
                if item.is_file():
                    shutil.copy2(item, target / item.name)
            shutil.rmtree(self_path)
            return target

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("shutil.which", return_value=None),
            patch.object(Path, "rename", patched_rename),
        ):
            with pytest.raises(PluginError, match="pip install failed"):
                await installer.install("https://example.com/plugin.git")

        final_dir = plugins_dir / "failing@0.1.0"
        assert not final_dir.exists(), "Plugin dir should be cleaned up on dep failure"


class TestInstallIncompatibleCoreApi:
    @pytest.mark.asyncio
    async def test_install_rejects_incompatible_core_api(self, tmp_path: Path):
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        installer = PluginInstaller(plugins_dir, AsyncMock())

        # Create a fake cloned repo whose manifest demands a future core version.
        repo_dir = plugins_dir / "_installing" / "repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "pack.yaml").write_text(
            "name: future\nversion: 0.1.0\nauthor: Test\n"
            "description: test\nrequires:\n  core_api: \">=99.0.0\"\n"
        )

        def mock_run(cmd, **kwargs):
            # git clone "succeeds" (repo already on disk); nothing else should run.
            return MagicMock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=mock_run):
            with pytest.raises(PluginError, match="core_api"):
                await installer.install("https://example.com/plugin.git")

        # Nothing should have been written to the final location.
        final_dir = plugins_dir / "future@0.1.0"
        assert not final_dir.exists()


class TestCheckPipDeps:
    def test_no_deps_returns_empty(self):
        missing = PluginLoader._check_pip_deps([])
        assert missing == []

    def test_installed_package_not_missing(self):
        from importlib.metadata import PackageNotFoundError

        def mock_version(name: str) -> str:
            if name == "requests":
                return "2.32.0"
            raise PackageNotFoundError(name)

        with patch("research_engine.plugins.loader.metadata_version", side_effect=mock_version):
            missing = PluginLoader._check_pip_deps(["requests>=2.28"])
        assert missing == []

    def test_missing_package_detected(self):
        from importlib.metadata import PackageNotFoundError

        with patch(
            "research_engine.plugins.loader.metadata_version",
            side_effect=PackageNotFoundError("playwright"),
        ):
            missing = PluginLoader._check_pip_deps(["playwright>=1.40"])
        assert missing == ["playwright"]

    def test_differing_import_name_resolved_without_override_table(self):
        # importlib.metadata resolves the *distribution*, so packages whose
        # import name differs from the pip name (e.g. pyyaml->yaml) are found
        # without any hand-rolled override table. pyyaml is a real dependency.
        missing = PluginLoader._check_pip_deps(["pyyaml>=6"])
        assert missing == []

    def test_mixed_installed_and_missing(self):
        from importlib.metadata import PackageNotFoundError

        def mock_version(name: str) -> str:
            if name == "requests":
                return "2.32.0"
            raise PackageNotFoundError(name)

        with patch("research_engine.plugins.loader.metadata_version", side_effect=mock_version):
            missing = PluginLoader._check_pip_deps(["requests>=2.28", "playwright>=1.40"])
        assert missing == ["playwright"]


class TestImportEntryCollision:
    def _loader(self, plugins_dir: Path) -> PluginLoader:
        return PluginLoader(AsyncMock(), PluginRegistry(), plugins_dir)

    def test_stdlib_shadow_rejected(self, tmp_path: Path):
        loader = self._loader(tmp_path)
        with pytest.raises(PluginLoadError, match="standard-library"):
            loader._import_entry(tmp_path, "code.tools.x:handler", "history")

    def test_cross_plugin_collision_rejected(self, tmp_path: Path):
        # Two plugin dirs both ship a top-level `recolwidget` package — the second
        # to load must be rejected rather than silently binding the first's code.
        pkg_name = "recolwidget"
        for plugin in ("plugin_a", "plugin_b"):
            pkg = tmp_path / plugin / pkg_name
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "tools.py").write_text(f"handler = '{plugin}'\n")

        loader = self._loader(tmp_path)
        try:
            handler = loader._import_entry(
                tmp_path / "plugin_a", f"{pkg_name}.tools:handler", "plugin_a"
            )
            assert handler == "plugin_a"
            with pytest.raises(PluginLoadError, match="already owned"):
                loader._import_entry(
                    tmp_path / "plugin_b", f"{pkg_name}.tools:handler", "plugin_b"
                )
        finally:
            for mod in list(sys.modules):
                if mod == pkg_name or mod.startswith(f"{pkg_name}."):
                    del sys.modules[mod]

    def test_same_plugin_multiple_entries_allowed(self, tmp_path: Path):
        # A plugin importing several entries from its own package is fine.
        pkg_name = "recolwidget2"
        pkg = tmp_path / "plugin_a" / pkg_name
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "a.py").write_text("handler = 'a'\n")
        (pkg / "b.py").write_text("handler = 'b'\n")

        loader = self._loader(tmp_path)
        try:
            assert loader._import_entry(tmp_path / "plugin_a", f"{pkg_name}.a:handler", "p") == "a"
            assert loader._import_entry(tmp_path / "plugin_a", f"{pkg_name}.b:handler", "p") == "b"
        finally:
            for mod in list(sys.modules):
                if mod == pkg_name or mod.startswith(f"{pkg_name}."):
                    del sys.modules[mod]


class TestManifestPipFields:
    def test_pip_fields_in_manifest(self, tmp_path: Path):
        from research_engine.plugins.manifest import parse_manifest

        yaml_content = """\
name: kindle
version: 0.1.0
author: MarginaliaAI
description: Kindle scraper

requires:
  pip:
    - "playwright>=1.40"
  setup_commands:
    - "playwright install chromium"

permissions:
  subprocess: true
"""
        manifest_path = tmp_path / "pack.yaml"
        manifest_path.write_text(yaml_content)

        m = parse_manifest(manifest_path)
        assert m.requires.pip == ["playwright>=1.40"]
        assert m.requires.setup_commands == ["playwright install chromium"]

    def test_pip_fields_default_empty(self):
        m = PluginManifest(
            name="test",
            version="0.1.0",
            author="Test",
            description="A test",
        )
        assert m.requires.pip == []
        assert m.requires.setup_commands == []
