"""Plugin installer — acquire a pack, install its dependencies, record it."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from research_engine.domain.errors import PluginError
from research_engine.domain.provenance import InstalledPlugin
from research_engine.plugins.compatibility import check_core_api
from research_engine.plugins.manifest import PluginManifest, parse_manifest

if TYPE_CHECKING:
    from collections.abc import Callable

    from research_engine.ports.repositories import InstalledPluginRepo

logger = structlog.get_logger()

#: Never copied into the plugins directory from a local source. `.git` because
#: a pack developed in-tree carries the whole engine history; `__pycache__`
#: because bytecode compiled against a different interpreter is worse than none.
_NOT_COPIED = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv")


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

    @staticmethod
    def _local_pack_dir(source: str) -> Path | None:
        """The pack directory `source` names, or None if it is not a local path.

        A pack under development lives in a working tree, not at a URL. Git
        URLs (`git@host:owner/repo.git`, `https://...`) never name a directory
        that exists, so an existence check separates the two without asking the
        caller to say which kind of source they have.
        """
        try:
            candidate = Path(source).expanduser()
        except (OSError, ValueError):
            return None
        return candidate.resolve() if (candidate / "pack.yaml").is_file() else None

    @staticmethod
    def _head_sha(directory: Path) -> str:
        """The commit `directory` is checked out at, or 'local' if untracked.

        A pack installed from a working tree still deserves a provenance
        record. When the tree is a git checkout the SHA says which revision is
        installed; when it isn't, the honest answer is that we cannot say.
        """
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "local"

    def _remove(self, path: Path) -> None:
        """Undo whatever `_place` did, whether it linked or copied.

        `shutil.rmtree` refuses a symlink outright, so a linked install that
        failed partway would otherwise leave a directory entry that blocks
        every later install of the same pack.
        """
        if path.is_symlink():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)

    async def install(
        self,
        source: str,
        ref: str = "main",
        console_callback: Callable[[str], None] | None = None,
        link: bool = False,
    ) -> InstalledPlugin:
        """Install a pack from a git URL or a local directory.

        `link` symlinks a local source instead of copying it, so edits in the
        working tree take effect on the next server start with no reinstall.
        It is the mode to develop a pack in; it has no meaning for a git URL,
        where there is no working tree to point at.
        """
        pack_dir = self._local_pack_dir(source)
        if pack_dir is not None:
            return await self._install_local(pack_dir, source, console_callback, link)
        if link:
            raise PluginError(
                f"--link needs a local directory containing pack.yaml, but "
                f"{source!r} is not one. Install from a git URL without --link."
            )
        return await self._install_from_git(source, ref, console_callback)

    async def _install_local(
        self,
        pack_dir: Path,
        source: str,
        console_callback: Callable[[str], None] | None,
        link: bool,
    ) -> InstalledPlugin:
        logger.info("plugin_install_local", path=str(pack_dir), link=link)
        if console_callback:
            console_callback(f"{'Linking' if link else 'Copying'} {pack_dir}...")

        manifest = parse_manifest(pack_dir / "pack.yaml")
        final_dir = self._prepare_destination(manifest)

        if link:
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            final_dir.symlink_to(pack_dir, target_is_directory=True)
        else:
            shutil.copytree(pack_dir, final_dir, ignore=_NOT_COPIED)

        return await self._finish(
            manifest,
            final_dir,
            source_url=str(pack_dir),
            source_ref=self._head_sha(pack_dir),
            console_callback=console_callback,
        )

    async def _install_from_git(
        self,
        source_url: str,
        ref: str,
        console_callback: Callable[[str], None] | None,
    ) -> InstalledPlugin:
        logger.info("plugin_install_clone", url=source_url, ref=ref)
        tmp_dir = self._plugins_dir / "_installing"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = tmp_dir / "repo"

        try:
            if console_callback:
                console_callback(f"Cloning {source_url}...")

            proc = subprocess.run(
                ["git", "clone", "--depth=1", f"--branch={ref}", source_url, str(repo_dir)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                raise PluginError(f"Git clone failed: {proc.stderr}")

            manifest = parse_manifest(repo_dir / "pack.yaml")
            commit_sha = self._head_sha(repo_dir)
            final_dir = self._prepare_destination(manifest)
            repo_dir.rename(final_dir)

            return await self._finish(
                manifest,
                final_dir,
                source_url=source_url,
                source_ref=commit_sha,
                console_callback=console_callback,
            )
        finally:
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)

    def _prepare_destination(self, manifest: PluginManifest) -> Path:
        """Check compatibility and that the slot is free, before touching disk."""
        incompat = check_core_api(manifest.requires.core_api)
        if incompat:
            raise PluginError(f"Plugin {manifest.name} is incompatible: {incompat}")

        final_dir = self._plugins_dir / f"{manifest.name}@{manifest.version}"
        # `exists()` follows symlinks and so answers False for a link whose
        # target is gone — which is still a name we cannot write to.
        if final_dir.exists() or final_dir.is_symlink():
            raise PluginError(
                f"Plugin {manifest.name}@{manifest.version} already installed at {final_dir}"
            )
        return final_dir

    async def _finish(
        self,
        manifest: PluginManifest,
        final_dir: Path,
        source_url: str,
        source_ref: str,
        console_callback: Callable[[str], None] | None,
    ) -> InstalledPlugin:
        """Install dependencies and record the pack, rolling back the files if either fails."""
        try:
            self._install_dependencies(manifest, console_callback)
        except Exception:
            self._remove(final_dir)
            raise

        plugin_record = InstalledPlugin(
            id=manifest.name,
            version=manifest.version,
            source_url=source_url,
            source_ref=source_ref,
            installed_at=datetime.now(UTC),
            enabled=True,
            # `mode="json"` because several manifest fields are `Path`:
            # every `file:` and `schema:` a pack declares. `installed_packs.
            # manifest` is a JSON column, and a plain model_dump leaves
            # PosixPath objects the driver cannot encode — so declaring an
            # extraction schema, an entity schema, or a tool schema made a
            # pack uninstallable, failing after its files were in place.
            manifest=manifest.model_dump(mode="json"),
            permissions_granted=manifest.permissions.model_dump(mode="json"),
        )
        try:
            await self._installed.insert(plugin_record)
        except Exception:
            # The files are already at their final location by this point.
            # Left there, they satisfy the "already installed" check on the
            # next attempt and the plugin can never be installed or removed
            # — `uninstall` needs the database row this failed to write.
            self._remove(final_dir)
            raise

        logger.info(
            "plugin_installed",
            plugin=manifest.name,
            version=manifest.version,
        )
        return plugin_record

    async def uninstall(self, plugin_id: str) -> None:
        """Uninstall a plugin."""
        plugin = await self._installed.get(plugin_id)
        if not plugin:
            raise PluginError(f"Plugin not found: {plugin_id}")

        # Remove directory
        plugin_dir = self._plugins_dir / f"{plugin.id}@{plugin.version}"
        if plugin_dir.exists() or plugin_dir.is_symlink():
            self._remove(plugin_dir)

        # Remove from DB
        await self._installed.delete(plugin_id)
        logger.info("plugin_uninstalled", plugin=plugin_id)

    async def enable(self, plugin_id: str) -> None:
        await self._installed.set_enabled(plugin_id, True)

    async def disable(self, plugin_id: str) -> None:
        await self._installed.set_enabled(plugin_id, False)
