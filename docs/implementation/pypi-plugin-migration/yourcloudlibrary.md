# YourCloudLibrary plugin implementation guide

**Repository:** `John-Cusack/marginalia-plugin-yourcloudlibrary`  
**Target release:** `marginalia-ai-plugin-yourcloudlibrary 0.3.0`  
**Prerequisite:** production core/SDK `0.6.0`  
**Current source state:** substantial uncommitted `0.2.0` catalog/source-provider work;
`pyproject.toml` still says `0.1.0`

## 0. Preserve and finish current work first

Do not reset or overwrite the current working tree. It contains the catalog search,
`source_search` provider, acquire/ingest path, integration tests, and a manifest version ahead
of package metadata.

Before packaging migration:

1. review all current tracked/untracked changes;
2. ensure scratch captures, cookies, `.env`, screenshots, and borrowed text remain ignored;
3. align the existing feature release to version `0.2.0` in package metadata/changelog;
4. run its current unit/integration gates;
5. commit/review that functional baseline separately.

Baseline commands after the feature baseline is coherent:

```bash
uv sync --extra dev --extra integration
uv run ruff check ycl tests
uv run pytest tests/unit -q
```

Live integration tests use a dedicated disposable test database and an authorized test loan;
never point them at the only research corpus.

## 1. Convert metadata for `0.3.0`

Update `pyproject.toml`:

- name/version `marginalia-ai-plugin-yourcloudlibrary`, `0.3.0`;
- dependency `marginalia-ai-sdk>=0.6,<0.7`;
- remove core from normal/integration dependency declarations that exist only for SDK imports;
- integration CI may install exact `marginalia-ai==0.6.0` explicitly;
- add complete README/license/authors/classifiers/keywords/URLs;
- add `research-engine-ycl-login = "ycl.cli.login:..."` console script with a synchronous
  zero-argument wrapper returning an exit code;
- add entry point:

```toml
[project.entry-points."research_engine.plugins"]
yourcloudlibrary = "ycl"
```

Decide Playwright packaging from measured behavior:

- preferred: base package contains HTTP runtime; `auth = ["playwright>=1.40"]` owns one-time
  login;
- if cookie refresh genuinely needs browser runtime after login, keep Playwright in base and
  state why in README.

Do not run Chromium installation during wheel install or plugin enable.

## 2. Move manifest/resources

Move root `pack.yaml` to `ycl/plugin.yaml` and convert to schema v2:

- `plugin_id: yourcloudlibrary`;
- core range `>=0.6,<0.7`;
- preserve actual permissions and verify network hosts against current live endpoints;
- preserve `ycl_book`, all current MCP tools, and source-search provider;
- include complete input schemas;
- remove duplicated identity/dependencies/setup commands.

Move any runtime schema/resource below `ycl/`. The wheel must contain all referenced files.

## 3. Migrate SDK and source-search imports

Replace `research_engine.plugins.sdk` with `research_engine_sdk`.

In `ycl/source_provider.py` replace core domain imports with SDK exports:

- `Availability`;
- `IngestAction`;
- `SourceMatch`;
- `SourceQuery`/provider protocol as applicable.

Unit tests import these from SDK. Remove `pytest.importorskip("research_engine")` where core is
not behavior under test.

Keep database-backed integration fixtures separate. They may install core `0.6.0` and use its
public testing integration contract, but must not import repositories/schema to assert plugin
unit behavior.

Gate: no runtime `research_engine.*` imports remain.

## 4. Replace direct core chunking

In `ycl/tools/ingest_book.py`:

- remove `ProseWindowChunker` and draft creation;
- preserve acquisition, cache, borrow registry, metadata, idempotency, expiry, and error logic;
- call SDK `ingestion.ingest_document()` with canonical text and `ycl_book`;
- let core apply manifest `prose_window`;
- retain source identity and document metadata exactly.

Update `acquire_and_ingest` to share the same ingestion boundary rather than duplicating a
second core-facing path.

Integration acceptance:

- one authorized fixture book stores canonical text and passages;
- repeat returns the existing document;
- failure to return a loan does not roll back already-ingested corpus data and is reported;
- no live book is borrowed in unit tests;
- cleanup removes exactly test-created rows and never touches the research corpus.

## 5. Move mutable state to plugin context

Replace `~/.marginalia/plugins/yourcloudlibrary` constants in `ycl/_paths.py` with
`PluginContext.data_dir`.

New default:

```text
~/.research-engine/plugin-data/yourcloudlibrary/
```

Update tools, borrow store, API cookie store, source provider, and login CLI to resolve one
shared context/config path.

Add an explicit migration command to the login/setup CLI:

- inventory old cookies, borrow registry, extracts, chapter sidecars, and partial files;
- show source/destination before action;
- refuse differing destination collisions;
- preserve active-session file permissions;
- verify reading the migrated registry/cookies before offering old-directory cleanup;
- never put extracted licensed content in package/release artifacts.

## 6. Authentication/setup command

Convert `ycl.cli.login` into an installed console script:

- synchronous `main()` wraps async implementation with `asyncio.run`;
- report missing `auth` extra with exact install command;
- report missing Chromium with explicit Playwright command;
- preserve visible interactive login, cookie validation, 15-minute timeout, and safe close;
- store cookies only under plugin data directory;
- redact cookie/JWT values from output/logs;
- return nonzero on failure.

Update runtime hints from `uv run python -m ycl.cli.login` to the installed command.

## 7. Discovery, approval, and loading

Add tests against core `0.6.0`:

1. wheel install yields `available` without importing `ycl`;
2. audit renders network/subprocess/filesystem/ingest permissions and all contributions;
3. enable stores version/hash/permissions;
4. load registers all MCP tools, `ycl_book`, and source-search provider atomically;
5. source-search returns SDK DTOs;
6. manifest/version changes require approval;
7. disabling leaves cookies/borrows/extracts untouched.

Permission documentation must say enabled code runs in-process and that borrowed/licensed text
must not be published.

## 8. Tests and documentation

Split tests:

- unit: SDK plus plugin only, no core/Postgres/network;
- contract: wheel resources, manifest, entry point, tool schemas, source-search DTOs;
- integration: exact core artifact and disposable test DB;
- live auth/provider: opt-in marker and existing authorized account only.

README must cover pip/pipx install, optional auth extra, Chromium/login, enable/audit, data
migration, catalog/loan workflow, licensed content, configuration, core compatibility,
security/support, and uninstall semantics.

Add changelog `0.3.0` for package rename, SDK cutover, source provider contract, data path,
entry-point activation, and removal of setup commands.

## 9. Release gate

```bash
uv lock
uv run ruff check ycl tests
uv run pytest tests/unit -q
uv build
uvx --from twine twine check --strict dist/*
```

Clean wheel smoke installs base without core source checkout, then exact core:

```bash
python -m venv /tmp/ycl-release-smoke
/tmp/ycl-release-smoke/bin/python -m pip install --upgrade pip
/tmp/ycl-release-smoke/bin/python -m pip install marginalia-ai==0.6.0 dist/*.whl
/tmp/ycl-release-smoke/bin/research-engine plugin list
```

Against disposable `RE_DB_URL`, enable plugin, verify `ycl.auth_status`, run fixture-backed
source search, and exercise an idempotent ingestion path. No test may expose cookie values,
borrow real material unintentionally, or leave corpus rows.

Tag `v0.3.0`, publish with Trusted Publishing, and repeat the artifact smoke from production
PyPI. The public wheel must contain no `.env`, cookies, browser profiles, scratch captures,
screenshots, or extracted books.
