# MarginaliaAI

**Give AI agents access to the books and papers you trust—not just what they
remember.**

Much of the information researchers rely on is not available on the open web.
It lives in personal and institutional libraries: books, journals, archives,
scanned documents, research collections, and licensed databases.

Most AI systems cannot reliably search those sources, retrieve the relevant
passages, and show exactly where an answer came from. MarginaliaAI turns sources
you are authorized to use into a searchable, citable corpus that AI agents can
access through the Model Context Protocol (MCP).

Instead of asking an agent what it remembers about a subject, you can let it
search the sources you trust, inspect the relevant text, and cite exact
locations in the canonical document.

> [!IMPORTANT]
> **MarginaliaAI 0.6 is a developer preview.** It requires PostgreSQL 15 or
> newer. Plugins are installed separately and must be explicitly audited and
> enabled. Integrations such as Logos and YourCloudLibrary use accounts you are
> authorized to access. MarginaliaAI does not redistribute licensed source
> content.

This README orients. Where it names something the code declares—commands,
tools, installed plugins—the code is authoritative: `research-engine --help`,
each plugin's `plugin.yaml`, and the migrations themselves.

## Quick Start

Install the public distribution:

```bash
python -m pip install marginalia-ai
research-engine --help
```

MarginaliaAI requires PostgreSQL 15 or newer with `vector`, `pg_trgm`, and
`ltree`. Follow the
[installed-package database setup](packages/core/README.md) to set
`RE_DB_URL`, upgrade the schema, and configure an MCP client.

From a source checkout (needs a Rust toolchain: `uv sync` compiles the
`research_engine._native` extension, and rebuilds it when crates change):

```bash
uv sync
make db          # starts Postgres and waits until it accepts connections
make migrate
uv run research-engine serve
```

The source workspace locks the three external public plugins in its default
`plugins` dependency group; History remains an in-workspace package. `uv sync`
therefore installs the same four-plugin family documented below. Installation
still does not enable or trust any plugin.

The source workflow creates an empty schema. The repository's
[corpus setup guide](docs/corpus-setup.md) documents one example corpus build.

## What it does

Two halves, joined by the corpus.

**Retrieval** — get material in, and find it again.

- **Ingestion pipeline** — pluggable document parsing with automatic chunking, embedding, and full-text indexing
- **Hybrid search** — fused keyword + vector retrieval with cross-encoder reranking
- **Structured extraction** — LLM-powered extraction of claims, references, and relationships from prose using declarative schemas
- **Entity resolution** — fuzzy name matching, alias tracking, and entity linking across documents
- **Event store** — temporal events with actors, locations, and typed payloads
- **Knowledge graph** — directed edges between documents, passages, entities, and events

**Authorship** — write from it, and keep what you wrote provable.

- **Span citations** — a citation is a `document_id` plus character offsets, not a remembered string
- **Verification** — `verify_quote` resolves a quotation against canonical text and reports how exactly it matches
- **Claim ledger** — claims, the evidence for each, and the edges between them
- **Freeze** — a work becomes rows when it is published, and stays checkable afterwards

## Works

The authorship half has a contract worth reading before you use it: **every
citation in a work is a structured span citation** — a `document_id` plus
character offsets into canonical text — so a citation is machine-checkable and
machine-derivable, never a string you have to remember the meaning of.

`verify_quote` resolves a citation against the text and answers `exact`,
`normalized`, `near`, or `not_found`. A work is review-ready only when every
citation verifies `exact` or `normalized`. `near` means the quote was edited
and needs re-anchoring; `not_found` means the citation is decoration rather
than evidence, and does not ship.

Claims use the same substrate. `claim_upsert` verifies and writes a claim, its
edges, and its anchors atomically; an unfindable quote refuses the whole call,
and an `asserts` anchor must name the person whose position it records.
`anchor_context` and `work_citations(context=true)` return the surrounding
canonical text, because matching characters alone does not prove a source bears
the interpretation placed on them.

Works are files under `works/` until their first freeze, then rows in
`authored.*`. The `work_*` MCP tools and `research-engine work` cover the
lifecycle; `research-engine work --help` lists it.

| Document | Purpose |
|----------|---------|
| [works/README.md](works/README.md) | The file contract — authoritative until a work's first freeze |
| [docs/design/works-architecture-master.md](docs/design/works-architecture-master.md) | Controlling architecture |

## Configuration

Create a `.env` file in the project root:

```env
RE_DB_URL=postgresql+asyncpg://re_dev:re_dev_pass@localhost:5435/research_engine
RE_ANTHROPIC_API_KEY=sk-ant-...
```

All settings use the `RE_` prefix. See `packages/core/src/research_engine/config/settings.py` for the full list.

## Plugin System

Corpus Engine discovers standard Python distributions through the
`research_engine.plugins` entry-point group. Installing a wheel makes a plugin
**available**; it does not import or enable the plugin. Core never runs pip,
clones Git repositories, copies plugin code, or executes manifest setup
commands.

### Published Plugins

These are the public plugins supported by the `0.6.x` SDK and core. Install
plugins into the same Python environment as `marginalia-ai`. The distribution
name used by pip is not necessarily the GitHub repository name.

| Distribution | Current release | Plugin ID | What it adds | Setup |
|--------------|-----------------|-----------|--------------|-------|
| [`marginalia-ai-plugin-history`](https://pypi.org/project/marginalia-ai-plugin-history/) | `0.2.0` | `history` | Correspondence schemas plus `history.find_missing_letters` and `history.correspondence_cadence` | None |
| [`marginalia-ai-plugin-logos`](https://pypi.org/project/marginalia-ai-plugin-logos/) | `0.2.1` | `logos` | Logos library search, passage and lexicon access, and licensed-book ingestion | [`auth` extra, Chromium, and Logos sign-in](https://github.com/John-Cusack/marginalia-plugin-logos#install) |
| [`marginalia-ai-plugin-academic-journal`](https://pypi.org/project/marginalia-ai-plugin-academic-journal/) | `0.2.1` | `academic-journal` | Scholarly discovery, open-access acquisition, paper search, and citation graphs | [Provider configuration and database migration](https://github.com/John-Cusack/marginalia-plugin-academic-journal#install) |
| [`marginalia-ai-plugin-yourcloudlibrary`](https://pypi.org/project/marginalia-ai-plugin-yourcloudlibrary/) | `0.3.0` | `yourcloudlibrary` | Library catalog search and borrowed-book acquisition and ingestion | [Chromium and library sign-in](https://github.com/John-Cusack/marginalia-plugin-yourcloudlibrary#install) |

Kindle is intentionally not published on PyPI. Do not infer that
`marginalia-ai-plugin-kindle` exists from architecture documents or old local
installations.

Install any subset, or all four:

```bash
python -m pip install \
  marginalia-ai-plugin-history \
  "marginalia-ai-plugin-logos[auth]" \
  marginalia-ai-plugin-academic-journal \
  marginalia-ai-plugin-yourcloudlibrary

# Logos sign-in and YourCloudLibrary use Playwright. Wheels never download a browser.
python -m playwright install chromium
```

For a pipx-managed core, inject plugins into that existing environment.
`--include-apps` exposes the plugins' login and diagnostic commands:

```bash
pipx inject marginalia-ai marginalia-ai-plugin-history
pipx inject --include-apps marginalia-ai "marginalia-ai-plugin-logos[auth]"
pipx inject marginalia-ai marginalia-ai-plugin-academic-journal
pipx inject --include-apps marginalia-ai marginalia-ai-plugin-yourcloudlibrary

# POSIX pipx environments:
"$(pipx environment --value PIPX_LOCAL_VENVS)/marginalia-ai/bin/python" \
  -m playwright install chromium
```

Authentication is provider-owned. It never happens during wheel installation
or plugin approval:

```bash
logos-login
research-engine-ycl-login
```

Review static manifests before importing plugin code, then enable the exact
artifacts. Only Logos and academic-journal currently own database migrations:

```bash
research-engine plugin list
research-engine plugin audit history
research-engine plugin audit logos
research-engine plugin audit academic-journal
research-engine plugin audit yourcloudlibrary

research-engine plugin enable history
research-engine plugin enable logos
research-engine plugin enable academic-journal
research-engine plugin enable yourcloudlibrary
research-engine plugin migrate logos
research-engine plugin migrate academic-journal
research-engine plugin doctor
```

Restart the MCP server after enabling or upgrading plugins. Enabled plugin
tools then appear in the MCP tool catalogue with their manifest descriptions.
Agents should use those descriptions rather than guess parameters. Common
entry points include `logos.search`, `logos.get_entry`,
`logos.ingest_book`, `academic-journal.discover_papers`,
`academic-journal.search_papers`, `yourcloudlibrary.search_catalog`, and
`yourcloudlibrary.acquire_and_ingest`.

Upgrades require approval of the new version and manifest hash:

```bash
python -m pip install --upgrade marginalia-ai-plugin-history
research-engine plugin approve-upgrade history
```

Disable before uninstalling. The approval audit row and plugin data remain
until explicitly forgotten or deleted:

```bash
research-engine plugin disable history
python -m pip uninstall marginalia-ai-plugin-history
research-engine plugin list        # reports the retained audit row as missing
research-engine plugin forget history
```

Editable installs replace the old link mode:

```bash
python -m pip install -e packages/plugins/history
research-engine plugin enable history
```

Plugin code executes in the Research Engine process after approval. Scoped
clients enforce the supported API and approved capabilities; they are not a
sandbox against malicious Python. Install and enable only trusted plugins.
Mutable plugin state is passed through `PluginContext.data_dir` under
`~/.research-engine/plugin-data/`, never stored in site-packages.

### Writing a Plugin

A plugin keeps its schema-v2 manifest inside its top-level import package and
publishes distribution identity, dependencies, authorship, license, and URLs
through `pyproject.toml`.

```toml
[project]
name = "marginalia-ai-plugin-mypack"
dependencies = ["marginalia-ai-sdk>=0.6,<0.7"]

[project.entry-points."research_engine.plugins"]
mypack = "mypack"
```

```yaml
# mypack/plugin.yaml
schema_version: 2
plugin_id: mypack
requires:
  core_api: ">=0.6,<0.7"
  python: ">=3.11"
permissions:
  network: none
  filesystem: plugin_data
provides:
  mcp_tools:
    - id: mypack.my_tool
      entry: mypack.tools.my_tool:handler
      description: What the tool does.
      input_schema:
        type: object
        properties:
          query: {type: string}
        required: [query]
```

Tool handlers import only the standalone SDK:

```python
from research_engine_sdk import CorpusClient, tool

@tool(
    id="mypack.my_tool",
    description="What the tool does",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)
async def handler(query: str, corpus: CorpusClient):
    return await corpus.find_passages(query, k=10)
```

See [packages/plugins/](packages/plugins/README.md) and the
[distribution architecture](docs/design/pypi-plugin-distribution-architecture.md).

## Project Structure

```text
packages/core/src/research_engine/
  config/           # Settings (pydantic-settings, .env loading)
  adapters/         # Postgres repos, embedding, reranker, LLM, HTTP
  services/         # Search, extraction, ingestion, entities, events
  plugins/          # Entry-point discovery, approval, loading, permissions
  mcp/              # MCP server + tool handlers
  cli/              # Command-line surface (`research-engine --help`)
  domain/           # Core domain models and error types
  ports/            # Repository protocols
packages/sdk/       # Plugin SDK distribution
packages/plugins/   # First-party plugin distributions in this workspace
```

## Documentation

| Document | Purpose |
|----------|---------|
| [03-architecture.md](corpus-engine-docs/docs/03-architecture.md) | System architecture and data flow |
| [05-mcp-spec.md](corpus-engine-docs/docs/05-mcp-spec.md) | MCP tool surface contract |
| [07-pack-system.md](corpus-engine-docs/docs/07-pack-system.md) | Pack manifest, SDK contract |
| [11-implementation-architecture.md](corpus-engine-docs/docs/11-implementation-architecture.md) | Implementation guide |
| [docs/corpus-setup.md](docs/corpus-setup.md) | Ordered sequence for populating the Bible corpus |
| [works/README.md](works/README.md) | The work file contract |
| [docs/pypi-readiness.md](docs/pypi-readiness.md) | PyPI readiness assessment, blockers, and release checklist |
| [docs/design/pypi-plugin-distribution-architecture.md](docs/design/pypi-plugin-distribution-architecture.md) | Proposed PyPI distribution, SDK, and plugin architecture |
| [docs/implementation/pypi-plugin-migration/index.md](docs/implementation/pypi-plugin-migration/index.md) | Executable cross-repository PyPI/plugin migration runbooks |

Vision, PRD, data model, roadmap and open questions are in
[corpus-engine-docs/](corpus-engine-docs/); decision records and
implementation notes in [docs/design/](docs/design/).

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Support

If this project saved you some time, you can buy me a coffee. It helps cover compute and API costs and keeps these tools free and maintained.

<a href="https://buymeacoffee.com/johncusack" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="41" width="174"></a>
