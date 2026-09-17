# Academic-journal plugin implementation guide

**Repository:** `John-Cusack/marginalia-plugin-academic-journal`  
**Target release:** `research-engine-plugin-academic-journal 0.2.0`  
**Prerequisite:** production SDK/core `0.6.0`  
**Critical baseline:** public `main` is incomplete; the local repository contains substantial
uncommitted authoritative implementation

## 0. Recover reviewed source before packaging

Do not build a release from `~/.research-engine/plugins` and do not discard the current local
working tree.

Current local changes include package metadata, package initializers, acquisition modules,
database code/migrations, filters, hooks, infrastructure, pipeline, tools, schemas, tests, and
manifest changes. The public remote contains only a small subset.

Recovery procedure:

1. inspect every tracked/untracked local file;
2. remove/ignore `.env`, caches, bytecode, downloaded PDFs, credentials, and local database
   artifacts from the proposed commit;
3. verify `tests/fixtures/sample.pdf` is synthetic/licensed for redistribution;
4. review SQL migrations and source-provider behavior;
5. run unit/integration tests against a disposable database;
6. commit and push the authoritative functional `0.1.x` source before starting packaging
   migration, or preserve it as a separately reviewable first commit in the migration branch;
7. confirm a fresh clone from the pushed commit contains every manifest-referenced module and
   schema.

Do not publish anything while the public repository cannot reproduce the installed plugin.

Baseline after recovery:

```bash
uv sync --extra dev
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv build --out-dir /tmp/acad-before
```

## 1. Normalize metadata and license

Update `pyproject.toml`:

- name/version `research-engine-plugin-academic-journal`, `0.2.0`;
- dependency `research-engine-sdk>=0.6,<0.7` plus actual httpx/asyncpg/Pydantic runtime needs;
- Apache-2.0 metadata matching repository `LICENSE`;
- README, license-file, authors, classifiers, keywords, source/issues/changelog URLs;
- entry point:

```toml
[project.entry-points."research_engine.plugins"]
academic-journal = "acad"
```

Add Ruff configuration/CI consistently rather than relying on another repo's environment.

Move root `pack.yaml` to `acad/plugin.yaml`, extraction schema under
`acad/schemas/extraction_schemas`, and SQL files remain/package under
`acad/db/migrations`. Convert manifest to v2, core `>=0.6,<0.7`, no pip/setup metadata.

## 2. Migrate SDK imports

Replace every core SDK import with `research_engine_sdk`.

In `acad/source_search.py`, use SDK `Availability`, `IngestAction`, `SourceMatch`,
`SourceQuery`, and provider protocol. In filters/hooks/tools use SDK contracts/decorators.

Unit tests must not import core domain/filter types merely to prove protocol conformance. Use
SDK runtime-checkable protocols/contracts.

Core-dependent integration tests may install `research-engine==0.6.0`, but should prefer
public service/adapter behavior over core repositories and schema internals. Where direct DB
verification is necessary, confine it to integration helpers and a disposable database.

Gate: zero runtime `research_engine.*` imports.

## 3. Define plugin data/config boundary

`acad/db/pool.py` currently searches project/current/home `.env`. Replace this with explicit
configuration supplied through SDK plugin context/environment:

- database URL comes from approved core/plugin database capability;
- mutable cache/download/job state uses `PluginContext.data_dir` at
  `~/.research-engine/plugin-data/academic-journal`;
- no upward/home `.env` discovery;
- secrets are never logged or packaged;
- acquisition download paths stay under plugin data unless caller explicitly supplies an
  approved path.

Add tests for explicit environment precedence, missing configuration, secret redaction, and
checkout-independent operation.

## 4. Version plugin database migrations

The current runner sorts SQL files and reapplies idempotent SQL with no revision/checksum
ledger. Convert it to the manifest-declared migration lifecycle.

Keep:

```text
acad/db/migrations/001_literature_pipeline.sql
acad/db/migrations/002_citation_graph.sql
```

Add plugin migration ledger, advisory lock, ordered transactional application, checksums,
status/current target reporting, and explicit upgrade entry. Remove `run_migrations()` calls
from every tool/hook; core `research-engine plugin migrate academic-journal` is the only
upgrade path.

Test:

- empty DB to revision 2;
- existing tables/data to revision 2 without count/content loss;
- repeat no-op;
- checksum drift refusal;
- concurrent migration serialization;
- plugin load refused while migration pending;
- downgrade is not implied or destructive.

## 5. Revalidate contributions against committed files

For every `acad/plugin.yaml` entry:

- import target exists in the wheel;
- tool has object input schema;
- filter/source-search/hook satisfies SDK contract;
- extraction schema resource exists;
- network host is in allowlist;
- declared permissions match behavior (`network`, `ingest`, `write`, filesystem, DB);
- background worker lifecycle cannot orphan unmanaged processes.

`acad.start_workers` deserves explicit treatment. Prefer a supervised core/plugin lifecycle or
foreground worker command over spawning hidden background processes from an MCP request. If it
still starts workers in-process, document ownership, shutdown, concurrency, and restart
semantics and test them.

## 6. Discovery, approval, loading

Against core `0.6.0` prove:

1. installed wheel appears `available` without importing `acad`;
2. audit shows all tools, filter, source provider, hook, schema, permissions, and DB revision;
3. enable records exact version/hash;
4. migrate reaches revision 2;
5. load registers all contributions atomically;
6. source search returns SDK DTOs and transient provider failures degrade to empty/provider
   diagnostics rather than breaking fan-out;
7. changed version/hash requires approval;
8. uninstall/missing distribution leaves literature tables and derived core edges intact.

## 7. Test architecture

Organize CI:

- unit: models, state machine, rate/circuit logic, citation extraction, SDK DTOs; no network/DB;
- contract: entry point, manifest/resources/tool schemas/filter/source-search/hook;
- integration: disposable PostgreSQL, migration, queue, discovery/acquisition, citation graph;
- live provider: opt-in credentials/network marker;
- artifact: wheel/sdist metadata and clean install.

Never skip the entire integration suite merely because core is absent; CI installs exact core
artifact. Never default integration to the research corpus.

## 8. Documentation and release workflow

Create README covering:

- package purpose and supported providers;
- pip/pipx installation;
- plugin enable/audit/migrate;
- provider API configuration and rate limits;
- plugin data/download storage;
- database tables/migration ownership;
- worker lifecycle;
- manual PDF acquisition and licensing;
- core/SDK compatibility;
- privacy/security/support.

Add changelog `0.2.0`. Add normal CI and protected Trusted Publishing workflow. Tag only after
fresh clone reproduces full source and tests.

## 9. Release gate

```bash
uv lock
uv run ruff check acad tests
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv build
uvx --from twine twine check --strict dist/*
```

Clean smoke:

```bash
python -m venv /tmp/acad-release-smoke
/tmp/acad-release-smoke/bin/python -m pip install --upgrade pip
/tmp/acad-release-smoke/bin/python -m pip install research-engine==0.6.0 dist/*.whl
/tmp/acad-release-smoke/bin/research-engine plugin list
```

Against disposable `RE_DB_URL`, enable, migrate, load, perform fixture-backed discovery, ingest
a synthetic/open-access fixture, and verify citation edge creation. Assert exact cleanup and no
live-provider credentials required.

Artifact must exclude `.env`, caches, downloaded paywalled PDFs, worker state, and local DB
files. Compare artifact file list to the fresh public checkout.

Tag `v0.2.0`, publish through Trusted Publishing, then repeat production-PyPI install →
discover → enable → migrate → load. Publication is blocked until the authoritative source is
committed and public/reviewable.
