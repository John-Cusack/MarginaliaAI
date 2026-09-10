# Corpus Engine

Corpus Engine turns a personal library into a research superpower by making every document deeply queryable, extractable, and cross-linkable through an AI agent.

It ingests heterogeneous sources (PDFs, EPUBs, HTML, Markdown, TEI-XML, and more), indexes them with rich metadata and vector embeddings, and exposes the full corpus to Claude Code and other MCP-capable agents through a well-designed tool interface.

> This file orients. Where it names something the code declares — commands,
> tools, installed packs — the code is authoritative: `research-engine --help`,
> each pack's `pack.yaml`, and the migrations themselves.

## Status

A running system with a populated corpus, not a scaffold. The schema is at
migration head `017`. For live counts, and the models and packs this engine is
currently wired to:

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

Corpus Engine is extended through **packs** — self-contained plugins that
contribute document types, entity types, extraction schemas, MCP tools,
ingestion modules, chunkers, filter extensions, and post-ingestion hooks.

```bash
# Install from a git URL, or from a local directory
uv run research-engine plugin install https://github.com/user/my-pack.git
uv run research-engine plugin install packages/plugins/history --link

# Inspect and manage what is installed
uv run research-engine plugin list
uv run research-engine plugin enable <name>
uv run research-engine plugin disable <name>
uv run research-engine plugin uninstall <name>
```

`--link` symlinks a local working tree instead of copying it, so edits take
effect on the next server start with no reinstall. It is the mode to develop a
pack in, and it has no meaning for a git URL.

Packs that ship with the engine live in
[packages/plugins/](packages/plugins/README.md). They get no privileges from
being in-tree — the loader resolves them through `~/.research-engine/plugins`
like any other, and the same permission gating applies. Packs that wrap a
third-party system, and so need their own release cadence, live in their own
repositories. `plugin list` shows what is actually installed here.

See [07-pack-system.md](corpus-engine-docs/docs/07-pack-system.md) for the full pack specification and SDK contract.

### Writing a Pack

A minimal pack needs a `pack.yaml` manifest and one or more tool handlers:

```yaml
# pack.yaml
name: my-pack
version: "0.1.0"
author: Your Name
description: What this pack does

requires:
  core_api: ">=0.1.0,<1.0.0"

provides:
  mcp_tools:
    - id: mypack.my_tool
      entry: "mypack.tools.my_tool:handler"
      description: What the tool does
```

> The top-level Python package name (`mypack` above) must be globally unique
> across installed packs and must not shadow a stdlib module (`code`, `json`,
> …) — name it after your pack. The loader rejects collisions to prevent one
> pack silently importing another's code.

Tool handlers receive scoped clients for core services:

```python
from research_engine.plugins.sdk import tool

@tool(
    id="mypack.my_tool",
    description="What the tool does",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)
async def handler(query: str, corpus=None, entity=None, **kwargs):
    results = await corpus.find_passages(query, k=10)
    return {"results": results}
```

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

Vision, PRD, data model, roadmap and open questions are in
[corpus-engine-docs/](corpus-engine-docs/); decision records and
implementation notes in [docs/design/](docs/design/).

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Support

If this project saved you some time, you can buy me a coffee. It helps cover compute and API costs and keeps these tools free and maintained.

<a href="https://buymeacoffee.com/johncusack" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="41" width="174"></a>
