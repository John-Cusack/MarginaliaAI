# Corpus Engine

Corpus Engine turns a personal library into a research superpower by making every document deeply queryable, extractable, and cross-linkable through an AI agent.

It ingests heterogeneous sources (PDFs, EPUBs, HTML, Markdown, TEI-XML, and more), indexes them with rich metadata and vector embeddings, and exposes the full corpus to Claude Code and other MCP-capable agents through a well-designed tool interface.

> This file orients. Where it names something the code declares — commands,
> tools, installed packs — the code is authoritative: `research-engine --help`,
> each plugin's `plugin.yaml`, and the migrations themselves.

## Status

A running system with a populated corpus, not a scaffold. The schema is at
migration head `019_plugin_activations`. For live counts, and the models and
plugins this engine is currently wired to:

```bash
uv run research-engine status
```

The corpus in active use is the Hebrew Bible across three editions — WLC, LHB
and ESV — over a Strong's-indexed word table. Building one from empty is an
ordered sequence; see [docs/corpus-setup.md](docs/corpus-setup.md).

## Quick Start

```bash
uv sync
make db          # starts Postgres and waits until it accepts connections
make migrate
uv run research-engine serve
```

Use `make db` rather than `docker compose up -d` directly: compose returns
before Postgres is ready, and the migration then races it. `make help` lists
the other targets.

That leaves you with an empty schema at head. Filling it is a separate, ordered
sequence — see [docs/corpus-setup.md](docs/corpus-setup.md) for the Bible
corpus, whose six steps must run in order because later ones validate against
rows the earlier ones write.

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

```bash
# Install into the same environment as marginalia-ai.
python -m pip install marginalia-ai-plugin-history
# pipx users inject into the core environment.
pipx inject marginalia-ai marginalia-ai-plugin-history

# Inspect the static manifest before any plugin code is imported.
research-engine plugin list
research-engine plugin audit history
research-engine plugin enable history

# Upgrades require approval of the new version and manifest hash.
python -m pip install --upgrade marginalia-ai-plugin-history
research-engine plugin approve-upgrade history

# Disable, then let the environment's package manager uninstall.
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

```
packages/core/src/research_engine/
  config/           # Settings (pydantic-settings, .env loading)
  adapters/         # Postgres repos, embedding, reranker, LLM, HTTP
  services/         # Search, extraction, ingestion, entities, events
  plugins/          # Pack loader, registry, SDK, permissions
  mcp/              # MCP server + tool handlers
  cli/              # Command-line surface (`research-engine --help`)
  domain/           # Core domain models and error types
  ports/            # Repository protocols
packages/sdk/       # Plugin SDK distribution
packages/plugins/   # First-party packs that ship with the engine
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
