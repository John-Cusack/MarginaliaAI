# First-party plugin distributions

Each directory here is an independently buildable Python distribution. An
installed plugin advertises exactly one top-level package through the
`research_engine.plugins` entry-point group and keeps its schema-v2
`plugin.yaml` inside that package.

First-party placement grants no runtime privileges. Discovery reads wheel
metadata and `plugin.yaml` without importing plugin code. An operator must
review and approve the exact distribution version, manifest hash,
contributions, permissions, and database declaration before core imports it.
Enabled plugins execute in-process and must be trusted.

## Development install

```bash
python -m pip install -e packages/plugins/history
research-engine plugin list
research-engine plugin audit history
research-engine plugin enable history
```

Core never installs, copies, links, or removes plugin code. Use pip, uv, or
pipx for package lifecycle:

```bash
python -m pip install --upgrade marginalia-ai-plugin-history
research-engine plugin approve-upgrade history
research-engine plugin disable history
python -m pip uninstall marginalia-ai-plugin-history
```

Removing a distribution retains its activation audit row and corpus data.
`plugin list` reports it as `missing`; `plugin forget history --yes` removes
only the activation row. Legacy executable directories under
`~/.research-engine/plugins` are reported by `plugin doctor`, never loaded or
deleted.

## Edition keys on ingested documents

A pack that knows its material's edition key writes it into the document
draft's `metadata["edition_key"]` at ingest. Nothing in core enforces this;
`work_verify` reports a citation's key against the cited document's, so a
pack that skips it makes every citation of its documents warn
`AUTH_EDITION_KEY_UNKNOWN`. The column is `json`, so the key reads back with
`metadata->>'edition_key`. The key is a plain string naming the edition —
it never touched Zotero's servers, and no account is involved; packs whose
authors keep a Zotero library typically use its keys here.
