# First-party packs

Packs that ship with the engine live here, one directory each, with a
`pack.yaml` at its root. They use the same plugin SDK as a pack from any
other source and get no privileges from being in this tree — the loader
resolves them through `~/.research-engine/plugins` like everything else,
and the permission gating in `plugins/permissions.py` applies unchanged.

Being in-tree buys one thing: a pack and the SDK change it depends on move
in the same commit, reviewed together. `11-implementation-architecture.md`
calls this monorepo-resident, and says factoring a pack out into its own
repo is straightforward once its interface has stabilized. The packs that
have already gone that way — logos, academic-journal, kindle,
yourcloudlibrary — each wrap a third-party system that breaks on its own
schedule and so needs its own release cadence. A pack with no external
dependency has nothing to gain from the split.

## Installing one

    research-engine plugin install packages/plugins/history --link

`--link` symlinks the working tree, so edits take effect on the next
server start with no reinstall. Drop it to copy instead, which is what you
want when installing a pack you are not editing.

Either way the engine records where the pack came from and, when that
directory is a git checkout, which commit it was at.

## Removing one

    research-engine plugin uninstall history

For a linked pack this removes the link only. The working tree is left
alone.
