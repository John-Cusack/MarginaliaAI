"""Plugin installer — git clone, dependency install, install/uninstall."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from research_engine.domain.errors import PluginError
from research_engine.domain.provenance import InstalledPlugin
from research_engine.plugins.compatibility import check_core_api
from research_engine.plugins.manifest import PluginManifest, parse_manifest

if TYPE_CHECKING:
    from pathlib import Path

    from research_engine.ports.repositories import InstalledPluginRepo

logger = structlog.get_logger()


class PluginInstaller:
    def __init__(
        self,
        plugins_dir: Path,
        installed_repo: InstalledPluginRepo,
    ) -> None:
        self._plugins_dir = plugins_dir
        self._installed = installed_repo

    @staticmethod
    def _resolve_pip_command() -> list[str]:
        """Return the pip install command, preferring uv if available."""
        if shutil.which("uv"):
            return ["uv", "pip", "install"]
        return [sys.executable, "-m", "pip", "install"]

    def _install_dependencies(
        self,
        manifest: PluginManifest,
        callback: Callable[[str], None] | None = None,
    ) -> None:
        """Install pip dependencies and run setup commands from the manifest."""
        requires = manifest.requires

        # Step 1: pip dependencies
        if requires.pip:
            pip_cmd = self._resolve_pip_command()
            full_cmd = [*pip_cmd, *requires.pip]

            if callback:
                callback(f"Installing pip dependencies: {', '.join(requires.pip)}")
            logger.info("plugin_install_pip", plugin=manifest.name, deps=requires.pip)

            proc = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode != 0:
                raise PluginError(
                    f"pip install failed for {manifest.name}: {proc.stderr}"
                )

        # Step 2: setup commands
        if requires.setup_commands:
            if not manifest.permissions.subprocess:
                raise PluginError(
                    f"Plugin {manifest.name} declares setup_commands but "
                    f"permissions.subprocess is false. Add 'subprocess: true' "
                    f"to the permissions section of pack.yaml."
                )

            for cmd in requires.setup_commands:
                if callback:
                    callback(f"Running setup: {cmd}")
                logger.info("plugin_setup_command", plugin=manifest.name, cmd=cmd)

                proc = subprocess.run(
                    cmd,
                    shell=True,  # noqa: S602
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if proc.returncode != 0:
                    raise PluginError(
                        f"Setup command failed for {manifest.name}: "
                        f"'{cmd}' exited with code {proc.returncode}: {proc.stderr}"
                    )

    async def install(
        self,
        source_url: str,
        ref: str = "main",
        console_callback: Callable[[str], None] | None = None,
    ) -> InstalledPlugin:
        """Install a plugin from a git URL."""
        # Clone
        logger.info("plugin_install_clone", url=source_url, ref=ref)
        tmp_dir = self._plugins_dir / "_installing"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            if console_callback:
                console_callback(f"Cloning {source_url}...")

            proc = subprocess.run(
                ["git", "clone", "--depth=1", f"--branch={ref}", source_url, str(tmp_dir / "repo")],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                raise PluginError(f"Git clone failed: {proc.stderr}")

            repo_dir = tmp_dir / "repo"

            # Parse manifest
            manifest = parse_manifest(repo_dir / "pack.yaml")

            # Check core API compatibility before writing anything to disk
            incompat = check_core_api(manifest.requires.core_api)
            if incompat:
                raise PluginError(f"Plugin {manifest.name} is incompatible: {incompat}")

            # Get commit SHA
            sha_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            commit_sha = sha_proc.stdout.strip()

            # Move to final location
            final_dir = self._plugins_dir / f"{manifest.name}@{manifest.version}"
            if final_dir.exists():
                raise PluginError(
                    f"Plugin {manifest.name}@{manifest.version} already installed at {final_dir}"
                )
            repo_dir.rename(final_dir)

            # Install dependencies
            try:
                self._install_dependencies(manifest, console_callback)
            except Exception:
                shutil.rmtree(final_dir, ignore_errors=True)
                raise

            # Record installation
            plugin_record = InstalledPlugin(
                id=manifest.name,
                version=manifest.version,
                source_url=source_url,
                source_ref=commit_sha,
                installed_at=datetime.now(UTC),
                enabled=True,
                manifest=manifest.model_dump(),
                permissions_granted=manifest.permissions.model_dump(),
            )
            await self._installed.insert(plugin_record)

            logger.info(
                "plugin_installed",
                plugin=manifest.name,
                version=manifest.version,
            )
            return plugin_record

        finally:
            # Clean up temp dir
            if (tmp_dir / "repo").exists():
                shutil.rmtree(tmp_dir / "repo", ignore_errors=True)

    async def uninstall(self, plugin_id: str) -> None:
        """Uninstall a plugin."""
        plugin = await self._installed.get(plugin_id)
        if not plugin:
            raise PluginError(f"Plugin not found: {plugin_id}")

        # Remove directory
        plugin_dir = self._plugins_dir / f"{plugin.id}@{plugin.version}"
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)

        # Remove from DB
        await self._installed.delete(plugin_id)
        logger.info("plugin_uninstalled", plugin=plugin_id)

    async def enable(self, plugin_id: str) -> None:
        await self._installed.set_enabled(plugin_id, True)

    async def disable(self, plugin_id: str) -> None:
        await self._installed.set_enabled(plugin_id, False)
