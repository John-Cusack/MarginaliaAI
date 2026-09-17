# Logos plugin implementation guide

**Repository:** `John-Cusack/marginalia-plugin-logos`  
**Target release:** `marginalia-ai-plugin-logos 0.2.0`  
**Prerequisite:** final SDK/core `0.6.0` chunking and ingestion contracts  
**Current source state:** clean `main`, package `0.1.0`, no tags

## 0. Baseline

```bash
uv sync --extra dev
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv build --out-dir /tmp/logos-before
```

Run network/auth/live ingestion only with explicit credentials and markers. Record current
`logos.auth_status`, one read-only tool, chunker contract, and resumable-ingest tests.

## 1. Normalize package identity and license

Update `pyproject.toml`:

- name/version `marginalia-ai-plugin-logos`, `0.2.0`;
- depend on `marginalia-ai-sdk>=0.6,<0.7`, `httpx`, `asyncpg`, and runtime Pydantic needs;
- retain Playwright under `auth` extra;
- set Apache-2.0 to agree with repository `LICENSE`;
- add package README, license file metadata, authors/classifiers/keywords/URLs;
- preserve `logos-login` and `logos-diagnose` console scripts;
- add entry point:

```toml
[project.entry-points."research_engine.plugins"]
logos = "logos"
```

Replace root `pack.yaml` with `logos/plugin.yaml` schema v2. Move
`schemas/extraction_schemas` under `logos/schemas/extraction_schemas` and update manifest
resource paths.

Manifest v2 preserves actual permissions/contributions, declares core `>=0.6,<0.7`, and adds
its database migration contribution. Remove dependency/setup/identity duplication.

## 2. Migrate all SDK imports

Every runtime import of `research_engine.*` must disappear.

Mechanical replacements:

- decorators/protocols/types/errors → `research_engine_sdk`;
- source-search/filter contracts → SDK;
- passage/node drafts → SDK DTOs;
- embedding-unavailable handling → SDK public error;
- chunking helpers/token estimation → `research_engine_sdk.chunking`.

`logos/ingest/chunker.py` remains plugin-owned behavior but uses SDK `Chunker`, `PassageDraft`,
`cap_spans`, `split_at_boundary`, and token helpers. Preserve version `5.0` behavior unless a
consumer-visible boundary changes; if it changes, bump chunker version and document reindexing.

`logos/tools/ingest_book.py` must not import `build_node_tree` or core domain classes. Build SDK
passage/node drafts and hand them to `IngestionClient.ingest_drafts()` with canonical full text.

Tests stop checking out/inserting a sibling core on `sys.path` for unit work. Unit and chunker
contract tests use SDK only; exact core is installed only in integration jobs.

### Gate 2

- repository runtime search has zero `research_engine.*` imports;
- chunker contract passes on English, Greek, Hebrew, long lexicon paragraph, index, and overlap
  amplification fixtures;
- resumable ingestion produces SDK drafts whose text equals canonical slices;
- tool modules import with SDK/core wheel environment, not `PYTHONPATH`.

## 3. Replace hard-coded state paths

Current authentication uses `~/.logos-mcp`; change it to SDK `PluginContext.data_dir`, default:

```text
~/.research-engine/plugin-data/logos/
  cookies.json
  browser-profile/
```

Update `logos/lib/constants.py`, cookie store, manager, Playwright login, console commands, and
any checkpoints/files.

Add explicit `logos-login --migrate-data` behavior:

- inventory old session/profile data;
- refuse non-identical destination collisions;
- preserve restrictive file permissions;
- verify migrated session structure before cleanup;
- never log cookie values or copy credentials into artifacts.

The console scripts must resolve the same data location as plugin tools without importing core.

## 4. Make plugin database migrations explicit

Replace the lazy `_migrated`/`CREATE IF NOT EXISTS` call from every tool with a versioned
migration runner:

```text
logos/db/migrations/001_initial.sql
logos/db/migrate.py
```

`001_initial.sql` contains the existing table/index definitions and creates a plugin migration
ledger. The runner:

- exposes manifest-declared `status` and `upgrade` async entries;
- obtains an advisory lock;
- runs each unapplied migration transactionally;
- records revision/checksum/application time;
- rejects checksum drift for an applied revision;
- reports current/target revision;
- does not drop data.

Remove `run_migrations()` calls from tool handlers. Core `plugin migrate logos` performs the
explicit upgrade after approval and refuses normal plugin load when revisions are pending.

Test migration on empty DB and on a copy containing all existing `logos_*` tables/checkpoints.
Counts and stored progress must remain unchanged.

## 5. Permission and client boundaries

Audit every tool:

- HTTP that can use scoped `HttpClient` should do so; document any direct httpx need that
  cannot use it.
- Ingestion goes through SDK `IngestionClient`.
- Graph/entity/event access uses SDK clients only.
- Plugin DB access remains plugin-owned and uses the explicitly granted DB capability.
- Network allowlist in `plugin.yaml` covers actual Logos/Faithlife endpoints; avoid `full`
  network if a measured allowlist can support authentication and APIs.

Enabled plugins remain trusted in-process code; say so in README and audit output.

## 6. Package resources and discovery

Ensure wheel/sdist contain:

- `logos/plugin.yaml`;
- extraction schema YAML;
- numbered SQL migrations;
- parser/auth/tool modules;
- no cookies/profile/checkpoints/`.env`/test cache.

Against core `0.6.0`, prove:

1. discovery reads manifest without importing `logos`;
2. audit lists every tool/type/chunker/filter/schema/permission/migration;
3. enable records exact version/hash;
4. migrate upgrades plugin DB;
5. load atomically registers the entire pack;
6. `logos.get_entry`, auth status, and library/search tool ids appear;
7. upgrade/hash change requires approval;
8. failure of one contribution leaves none registered.

## 7. CI and documentation

Replace sibling-checkout CI with:

- SDK-only unit/chunker tests on Python 3.11–3.13;
- exact production/local SDK artifact install during pre-release;
- core `0.6.0` integration job with disposable PostgreSQL;
- artifact resource/metadata checks;
- opt-in authenticated live tests only;
- protected Trusted Publishing workflow.

README must cover pip/pipx install, `auth` extra, Playwright browser installation, login/status,
plugin enable/audit/migrate, data migration, database ownership, core compatibility, licensed
content, and security/support.

Add `CHANGELOG.md` with `0.2.0` breaking packaging/SDK/data/migration changes.

## 8. Release gate

```bash
uv lock
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv build
uvx --from twine twine check --strict dist/*
```

Clean smoke:

```bash
python -m venv /tmp/logos-release-smoke
/tmp/logos-release-smoke/bin/python -m pip install --upgrade pip
/tmp/logos-release-smoke/bin/python -m pip install marginalia-ai==0.6.0 dist/*.whl
/tmp/logos-release-smoke/bin/research-engine plugin list
```

With a disposable copy of plugin/core data:

- enable and migrate Logos;
- load all contributions;
- run chunker contract and one read-only fixture HTTP tool;
- run a resumable fixture ingest through SDK clients;
- assert canonical text, passage offsets, nodes, embeddings, and checkpoint state;
- assert no checkout on `PYTHONPATH` and no old code directory used.

Tag `v0.2.0`, publish through Trusted Publishing, then repeat install → discover → enable →
migrate → load from production PyPI. Do not publish until package/repository license metadata
and all artifacts agree on Apache-2.0.
