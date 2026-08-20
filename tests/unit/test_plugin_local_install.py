"""Installing a pack from a working tree, by copy and by symlink.

A pack under development lives in a directory, not at a URL. Before this the
installer could only `git clone`, so the only way to try a change to a pack was
to commit it, push it, and reinstall — which is why the first-party packs in
`packages/plugins/` had no supported way to be installed at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003  (pytest resolves fixture annotations at runtime)
from unittest.mock import AsyncMock

import pytest

from research_engine import __version__ as CORE_VERSION
from research_engine.domain.errors import PluginError
from research_engine.plugins.installer import PluginInstaller

PACK_YAML = f"""\
name: sample
version: 0.1.0
author: Test
description: A pack that lives in a working tree.
requires:
  core_api: "=={CORE_VERSION}"
"""


def _make_pack(root: Path) -> Path:
    """A minimal but real pack directory, with junk that must not be copied."""
    pack = root / "sample-pack"
    (pack / "sample").mkdir(parents=True)
    (pack / "pack.yaml").write_text(PACK_YAML)
    (pack / "sample" / "__init__.py").write_text("VALUE = 1\n")

    (pack / "sample" / "__pycache__").mkdir()
    (pack / "sample" / "__pycache__" / "stale.pyc").write_bytes(b"\x00")
    (pack / ".git").mkdir()
    (pack / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return pack


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.fixture
def installer(plugins_dir: Path) -> PluginInstaller:
    repo = AsyncMock()
    repo.insert = AsyncMock()
    return PluginInstaller(plugins_dir, repo)


class TestLocalPackDetection:
    def test_directory_with_a_manifest_is_local(self, tmp_path: Path):
        pack = _make_pack(tmp_path)
        assert PluginInstaller._local_pack_dir(str(pack)) == pack.resolve()

    def test_ssh_git_url_is_not_local(self):
        assert PluginInstaller._local_pack_dir("git@github.com:o/r.git") is None

    def test_https_git_url_is_not_local(self):
        assert PluginInstaller._local_pack_dir("https://github.com/o/r.git") is None

    def test_directory_without_a_manifest_is_not_local(self, tmp_path: Path):
        (tmp_path / "empty").mkdir()
        assert PluginInstaller._local_pack_dir(str(tmp_path / "empty")) is None

    def test_a_path_that_does_not_exist_is_not_local(self, tmp_path: Path):
        assert PluginInstaller._local_pack_dir(str(tmp_path / "nope")) is None


class TestCopyInstall:
    async def test_copies_the_pack_into_the_plugins_directory(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        record = await installer.install(str(pack))

        installed = plugins_dir / "sample@0.1.0"
        assert record.id == "sample"
        assert not installed.is_symlink()
        assert (installed / "pack.yaml").read_text() == PACK_YAML
        assert (installed / "sample" / "__init__.py").read_text() == "VALUE = 1\n"

    async def test_leaves_git_and_bytecode_behind(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        await installer.install(str(_make_pack(tmp_path)))

        installed = plugins_dir / "sample@0.1.0"
        assert not (installed / ".git").exists()
        assert not (installed / "sample" / "__pycache__").exists()

    async def test_a_copy_does_not_track_later_edits(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        await installer.install(str(pack))
        (pack / "sample" / "__init__.py").write_text("VALUE = 2\n")

        installed = plugins_dir / "sample@0.1.0" / "sample" / "__init__.py"
        assert installed.read_text() == "VALUE = 1\n"


class TestLinkInstall:
    async def test_links_rather_than_copies(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        await installer.install(str(pack), link=True)

        installed = plugins_dir / "sample@0.1.0"
        assert installed.is_symlink()
        assert installed.resolve() == pack.resolve()

    async def test_edits_in_the_working_tree_are_visible_through_the_link(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        await installer.install(str(pack), link=True)
        (pack / "sample" / "__init__.py").write_text("VALUE = 2\n")

        installed = plugins_dir / "sample@0.1.0" / "sample" / "__init__.py"
        assert installed.read_text() == "VALUE = 2\n"

    async def test_link_is_rejected_for_a_git_url(self, installer: PluginInstaller):
        with pytest.raises(PluginError, match="--link needs a local directory"):
            await installer.install("git@github.com:o/r.git", link=True)


class TestProvenance:
    async def test_records_the_commit_when_the_source_is_a_checkout(
        self, installer: PluginInstaller, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        (pack / ".git" / "HEAD").unlink()  # replace the fake .git with a real one
        (pack / ".git").rmdir()
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "T"],
            ["git", "add", "-A"],
            ["git", "commit", "-qm", "init"],
        ):
            subprocess.run(cmd, cwd=pack, check=True, capture_output=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=pack, capture_output=True, text=True
        ).stdout.strip()

        record = await installer.install(str(pack))
        assert record.source_ref == sha

    async def test_says_local_when_the_source_is_not_a_checkout(
        self, installer: PluginInstaller, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        (pack / ".git" / "HEAD").unlink()
        (pack / ".git").rmdir()

        record = await installer.install(str(pack))
        assert record.source_ref == "local"


class TestUninstall:
    async def test_removing_a_linked_pack_leaves_the_working_tree_alone(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        record = await installer.install(str(pack), link=True)
        installer._installed.get = AsyncMock(return_value=record)

        await installer.uninstall("sample")

        assert not (plugins_dir / "sample@0.1.0").is_symlink()
        assert (pack / "pack.yaml").is_file()
        assert (pack / "sample" / "__init__.py").read_text() == "VALUE = 1\n"


class TestAlreadyInstalled:
    async def test_a_second_install_is_refused(
        self, installer: PluginInstaller, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        await installer.install(str(pack))
        with pytest.raises(PluginError, match="already installed"):
            await installer.install(str(pack))

    async def test_a_dangling_link_still_counts_as_occupied(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        # A link whose target has moved: `exists()` says False, but the name is
        # taken, and symlink_to would raise FileExistsError from inside the
        # installer rather than reporting the real problem.
        (plugins_dir / "sample@0.1.0").symlink_to(tmp_path / "gone", target_is_directory=True)

        with pytest.raises(PluginError, match="already installed"):
            await installer.install(str(pack))


class TestRollback:
    async def test_a_failed_record_removes_the_link_not_the_source(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        installer._installed.insert = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(RuntimeError):
            await installer.install(str(pack), link=True)

        assert not (plugins_dir / "sample@0.1.0").is_symlink()
        assert (pack / "pack.yaml").is_file()

    async def test_a_failed_record_removes_a_copy(
        self, installer: PluginInstaller, plugins_dir: Path, tmp_path: Path
    ):
        pack = _make_pack(tmp_path)
        installer._installed.insert = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(RuntimeError):
            await installer.install(str(pack))

        assert not (plugins_dir / "sample@0.1.0").exists()
        assert (pack / "pack.yaml").is_file()
