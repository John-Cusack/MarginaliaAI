# Core, SDK, and history implementation guide

**Repository:** `John-Cusack/MarginaliaAI`  
**Target releases:** `research-engine-sdk 0.6.0`, `research-engine 0.6.0`,
`research-engine-plugin-history 0.2.0`  
**Depends on:** [architecture](../../design/pypi-plugin-distribution-architecture.md)  
**Produces:** the contracts every external plugin guide consumes

Run phases in order. A failed gate blocks every later repository.

## 0. Baseline and scope

Expected current facts:

- core and SDK build with Hatchling;
- SDK standalone import is broken because it conditionally re-exports core;
- core version is `0.5.0`; migration head is `018_anchor_editions`;
- plugin installation clones/copies code, installs dependencies, and executes setup commands;
- `installed_packs` stores Git/source identity;
- history is source-only under `packages/plugins/history`;
- base core directly requires sentence-transformers and Docling.

Record baseline:

```bash
uv sync --group dev
uv run ruff check packages/ tests/
uv run pytest tests/unit -q
uv run pytest packages/plugins -q
uv build --package research-engine --out-dir /tmp/re-core-before
uv build --package research-engine-sdk --out-dir /tmp/re-sdk-before
```

Do not change database or public contracts until these results are understood.

## 1. Build the real standalone SDK

### 1.1 Replace the current re-export package

Populate `packages/sdk/src/research_engine_sdk/` with:

```text
__init__.py
py.typed
manifest.py
types.py
clients.py
interfaces.py
decorators.py
errors.py
chunking.py
testing.py
```

Source the initial behavior from
`packages/core/src/research_engine/plugins/sdk/`, but remove every import of
`research_engine`.

### 1.2 Define the manifest v2 contract

In `manifest.py` define and export validated models for:

- `PluginManifest` with `schema_version == 2`, `plugin_id`, `requires`, `permissions`, and
  `provides`;
- core/Python compatibility;
- document/entity/event/relation types;
- ingestion modules, chunkers, schemas, tools, hooks, filter extensions, source-search
  providers, and optional database migration contribution;
- network/filesystem/LLM/ingest/write/subprocess permissions.

Manifest v2 must reject legacy-only fields:

- identity/version/author/license/homepage;
- `requires.pip`;
- `requires.setup_commands`.

Resource paths must be relative, remain inside the plugin package, and reject `..` traversal.

### 1.3 Define public types and clients

Move or define SDK-owned DTOs required by existing plugins:

- `PassageDraft`, `NodeDraft`, `ParsedDocument`, `SourceRef`, `DetectionResult`;
- `EventFilter` and event result shapes used by `EventClient`;
- `Availability`, `IngestAction`, `SourceQuery`, `SourceMatch`, `SourceSearchProvider`;
- `PluginContext(plugin_id, data_dir, distribution_name, distribution_version)`;
- public errors including `PermissionDenied`, `PluginConfigError`, validation/configuration
  errors, and the embedding-unavailable error exposed through ingestion.

`clients.py` must include an ingestion method that removes the need for built-in chunker
imports:

```python
async def ingest_document(
    self,
    *,
    title: str,
    document_type: str,
    text: str,
    source: str = "",
    metadata: dict | None = None,
    language: str | None = None,
    sections: list[dict] | None = None,
) -> dict: ...
```

Retain `ingest_drafts()` for custom chunkers such as Logos. Its drafts and nodes are SDK DTOs.

`EventClient.query()` accepts the SDK `EventFilter` and returns SDK/event DTOs, so history no
longer imports a core domain model.

### 1.4 Move stable chunking contracts

Move the public algorithms required by custom chunkers into `research_engine_sdk.chunking` as
one source of truth:

- script-aware token estimation;
- `split_at_boundary`, `cap_spans`, and related invariant-preserving helpers;
- chunker protocol/DTO;
- absolute token ceiling constants used by contract tests.

Update core chunkers to import these functions from SDK. Do not leave duplicated algorithms in
core and SDK.

### 1.5 SDK package metadata

Update `packages/sdk/pyproject.toml`:

- distribution/version `research-engine-sdk==0.6.0`;
- package README and Apache-2.0 license file;
- authors, classifiers, keywords, source/issues/changelog URLs;
- `pydantic` and other genuinely standalone dependencies only;
- explicit Hatchling minimum supporting license metadata;
- include `py.typed`.

Create SDK tests under `packages/sdk/tests/` for manifest validation, decorators, DTOs,
protocol mocks, chunking contracts, and public exports.

### Gate 1

```bash
uv build --package research-engine-sdk --out-dir /tmp/re-sdk-060
uvx --from twine twine check --strict /tmp/re-sdk-060/*
uv run --isolated --no-project \
  --with /tmp/re-sdk-060/research_engine_sdk-0.6.0-py3-none-any.whl \
  python -c 'import research_engine_sdk as s; [getattr(s, n) for n in s.__all__]'
```

The environment must not contain `research-engine`.

## 2. Cut core over to SDK

### 2.1 Dependency and imports

Update `packages/core/pyproject.toml` to depend on `research-engine-sdk>=0.6,<0.7`; keep the
workspace source mapping at the root.

Migrate every core caller to `research_engine_sdk`. Delete
`packages/core/src/research_engine/plugins/sdk/` after the last caller moves. Do not retain
re-exports.

Where core needs richer internal models, adapt at the boundary rather than adding repository or
SQL concerns to SDK.

### 2.2 Add scoped adapters

Create concrete adapters instead of injecting raw services:

- `adapters/ingestion_client.py` implements SDK `IngestionClient`;
- `adapters/event_client.py` implements SDK `EventClient`;
- update existing corpus/extraction/edge adapters to import SDK protocols/DTOs;
- add entity and other adapters where current raw service shapes do not match SDK.

Implement `ingest_document()` through the existing orchestrator:

1. resolve the document type's default chunker from the registry;
2. chunk full text in core;
3. build nodes from SDK sections;
4. call the existing transactional draft ingestion path with canonical text;
5. return consumer-facing ids/counts.

Update `DeniedIngestionClient` with the same method.

Update `PluginLoader.build_plugin_clients()` so every injected object satisfies the SDK shape
and add `context` with the plugin data directory.

### Gate 2

- Core tests import no module under the deleted SDK path.
- A fixture handler receives only SDK-shaped clients/context.
- Kindle/YCL-style full-text ingest is proven without importing a core chunker.
- History-style event query is proven with SDK `EventFilter`.

Run:

```bash
uv run ruff check packages/ tests/
uv run pytest tests/unit/test_corpus_client_adapter.py tests/unit/test_dispatch.py -q
uv run pytest tests/unit/services/test_ingestion_orchestrator.py -q
```

## 3. Implement entry-point discovery without import

Create:

```text
packages/core/src/research_engine/plugins/discovery.py
packages/core/src/research_engine/plugins/activation.py
```

Rewrite `plugins/loader.py` around discovered distributions rather than filesystem copies.

### 3.1 Discovery behavior

Use `importlib.metadata.entry_points(group="research_engine.plugins")`. For each entry point:

1. require one top-level module value with no attribute/extras;
2. read distribution name/version/URLs/direct URL metadata;
3. locate `<module>/plugin.yaml` through distribution files;
4. read and parse without importing the module;
5. verify entry-point name equals `plugin_id`;
6. validate resources and entry modules remain in that package;
7. calculate manifest SHA-256;
8. return a `DiscoveredPlugin` record.

Tests must install a fixture wheel whose package raises if imported; discovery must still pass.

Reject duplicate ids, stdlib package names, invalid distributions, absent resources, and
incompatible core/Python ranges with deterministic reasons.

### 3.2 Atomic registry staging

Add staging/commit support to `plugins/registry.py`:

- create an isolated stage seeded with core-owned definitions;
- import and validate all contributions into the stage;
- detect conflicts against live registry and other stages;
- commit only after every contribution passes;
- discard stage on error.

A failing history-like tool schema must leave no document types, vocabularies, schemas, or tools
behind.

### Gate 3

Add focused discovery/loader tests for:

- no-import discovery;
- duplicate ids;
- path traversal and missing resources;
- incompatible versions;
- entry-point/manifest mismatch;
- missing tool schema;
- atomic rollback;
- two valid plugins loaded in deterministic order.

## 4. Replace installed-pack persistence

Create migration `019_plugin_activations.py` after `018_anchor_editions`.

Migration behavior:

1. rename `core.installed_packs` to `core.plugin_activations`;
2. rename `id` to `plugin_id` and `version` to `distribution_version`;
3. rename `source_url/source_ref` to `legacy_source_url/legacy_source_ref` and make them nullable;
4. add nullable `distribution_name`, `entry_point_name`, `manifest_sha256`, `approved_at`,
   `last_seen_at`, `last_error`, and `provenance`;
5. add non-null `state` defaulting to `legacy` and non-null
   `approved_non_interactive` defaulting false;
6. set every migrated row to `enabled=false`, `state='legacy'`;
7. retain manifest, permissions, install time, and source provenance;
8. implement downgrade restoring the original columns/data shape without deleting rows.

Update:

- `adapters/storage/postgres/schema.py`;
- domain model to `PluginActivation`;
- repository protocol and `repositories/plugins.py`;
- schema-truthfulness and migration round-trip tests.

New rows require distribution identity, version, entry point, manifest hash, snapshot, granted
permissions, and approval time. App code enforces the fields that remain nullable solely for
legacy audit rows.

### Gate 4

Run upgrade/downgrade/upgrade on a disposable copy containing all five current plugin rows.
Assert:

- all five rows survive;
- all are disabled legacy rows;
- manifests/permissions/source data are byte-for-byte equivalent JSON values;
- no legacy executable directory is read or imported.

## 5. Replace plugin CLI and installer

Rewrite `cli/plugin.py` around discovery/activation. Remove `plugins/installer.py` and its tests.

Implement:

```text
plugin list
plugin audit ID
plugin enable ID [--yes]
plugin approve-upgrade ID [--yes]
plugin disable ID
plugin migrate ID [--yes]
plugin doctor [ID]
plugin forget ID [--yes]
```

Behavior:

- newly installed distribution appears `available`, never enabled;
- enable prints distribution metadata, manifest hash, contributions, permissions, and database
  migration declaration before confirmation;
- non-interactive enable/upgrade refuses without `--yes`;
- approval records exact version/hash/permissions;
- version/hash change becomes `pending_approval`;
- missing distribution becomes `missing` without deleting the row;
- `forget` removes only activation/audit state and never invokes pip;
- unknown plugin output gives exact pip and pipx commands;
- legacy rows/directories are reported but never loaded or deleted.

Delete Git clone, runtime pip, local-copy/symlink, and setup-command behavior. Update root and pack
documentation accordingly.

### Gate 5

Run the focused plugin CLI/state-machine tests. Exercise install of a fixture wheel with pip,
then list → enable → load → upgrade → pending → approve → disable → uninstall → missing.

## 6. Add explicit plugin database migration lifecycle

Manifest v2 database contributions contain current revision and upgrade/status entries.

Implement `plugin migrate` so it:

- requires prior approval;
- imports only that approved migration entry;
- passes SDK `PluginContext` and database URL/connection capability deliberately;
- records current revision/status;
- refuses plugin load while declared migrations are pending;
- never automatically drops data.

Keep core and plugin schemas independent. Add a fixture plugin migration contract test.

## 7. Split core dependencies

Update `packages/core/pyproject.toml` using the extras in the architecture:

- remove `tiktoken`;
- move sentence-transformers to `local-inference`;
- move PyMuPDF/ebooklib/BeautifulSoup/lxml to `documents`;
- move Docling to `document-ai`;
- move OpenAI to `openai`;
- make embed server explicitly depend on its runtime.

Move local model imports inside local branches in `adapters/inference/routing.py`. Register
Docling conditionally in `composition._register_builtin_modules`.

Missing extras raise actionable errors. Remote inference never imports local inference.

### Gate 7

Build base core and install outside checkout. Assert package metadata has no heavy requirements
and runtime has no heavy modules:

```bash
python -c 'from importlib.util import find_spec; assert find_spec("torch") is None; assert find_spec("docling") is None; assert find_spec("sentence_transformers") is None'
research-engine --help
research-engine config --help
```

Also smoke each extra independently and `full`.

## 8. Package history as the reference plugin

Add `packages/plugins/history/pyproject.toml` and make it a workspace member. Target
`research-engine-plugin-history==0.2.0`.

Move:

```text
pack.yaml -> history/plugin.yaml
schemas/  -> history/schemas/
```

Use manifest v2 and entry point:

```toml
[project.entry-points."research_engine.plugins"]
history = "history"
```

Depend on `research-engine-sdk>=0.6,<0.7`; include README and Apache license. Update all imports
to SDK. Replace the core `EventFilter` import in `find_missing_letters.py` with SDK type/client
usage.

Keep both tool input schemas complete. Add artifact tests and an end-to-end test proving pip
install → available → enable → both tools registered. This is the reference implementation
external repositories follow.

## 9. Metadata, docs, and release workflows

Complete package metadata/README/license for SDK, core, and history. Single-source versions and
cut changelog sections for `0.6.0`/history `0.2.0`.

Add `.github/workflows/release.yml` with separate SDK/core/history build artifacts and publish
jobs. SDK publishes first; core depends on it; history publishes only after both production
artifacts pass smoke tests.

Update `.mcp.json` examples for installed use:

```json
{"type":"stdio","command":"research-engine","args":["serve"]}
```

Source-development configuration may still use `uv run`, but public installation must not
require a checkout or repository `cwd`.

## 10. Final core release gate

```bash
uv run ruff check packages/ tests/
uv run pytest tests/unit -q
uv run pytest packages/plugins -q
uv build --package research-engine-sdk --out-dir dist/sdk
uv build --package research-engine --out-dir dist/core
uv build --package research-engine-plugin-history --out-dir dist/history
uvx --from twine twine check --strict dist/sdk/* dist/core/* dist/history/*
```

Against a disposable PostgreSQL database and clean environment:

1. install the exact SDK/core/history wheels;
2. upgrade empty database through migration 019;
3. discover history without importing it;
4. approve and load history;
5. run both history tools against deterministic fixtures;
6. verify no legacy directory was executed;
7. start MCP with the installed console script;
8. upgrade a copy of the `0.5.0` database and prove all legacy plugin audit rows survived.

Publish only after every gate passes. The handoff to external repositories is the production
SDK/core `0.6.0` artifact pair plus their generated/public contract documentation.
