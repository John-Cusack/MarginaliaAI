# Corpus Engine

Corpus Engine turns a personal library into a research superpower by making every document deeply queryable, extractable, and cross-linkable through an AI agent.

It ingests heterogeneous sources (PDFs, EPUBs, HTML, Markdown, TEI-XML, and more), indexes them with rich metadata and vector embeddings, and exposes the full corpus to Claude Code and other MCP-capable agents through a well-designed tool interface.

## Core Capabilities

- **Hybrid search** — fused keyword + vector retrieval with cross-encoder reranking
- **Structured extraction** — LLM-powered extraction of claims, references, and relationships from prose using declarative schemas
- **Entity resolution** — fuzzy name matching, alias tracking, and entity linking across documents
- **Event store** — temporal events with actors, locations, and typed payloads
- **Knowledge graph** — directed edges between documents, passages, entities, and events
- **Ingestion pipeline** — pluggable document parsing with automatic chunking, embedding, and full-text indexing

## Quick Start

```bash
# Start the development database
docker compose -f tools/dev-postgres/docker-compose.yml up -d

# Run database migrations
uv run alembic -c packages/core/src/research_engine/adapters/storage/postgres/migrations/alembic.ini upgrade head

# Start the MCP server
uv run research-engine serve
```

## Configuration

Create a `.env` file in the project root:

```env
RE_DB_URL=postgresql+asyncpg://re_dev:re_dev_pass@localhost:5435/research_engine
RE_ANTHROPIC_API_KEY=sk-ant-...
```

All settings use the `RE_` prefix. See `packages/core/src/research_engine/config/settings.py` for the full list.

## Plugin System

Corpus Engine is extended through **packs** — self-contained plugins distributed as git repositories. Packs can contribute document types, entity types, extraction schemas, MCP tools, ingestion modules, and post-ingestion hooks.

```bash
# Install a pack from a git URL
uv run research-engine plugin install https://github.com/user/my-pack.git

# List installed packs
uv run research-engine plugin list

# Enable / disable
uv run research-engine plugin enable <name>
uv run research-engine plugin disable <name>
```

See [07-pack-system.md](corpus-engine-docs/docs/07-pack-system.md) for the full pack specification and SDK contract.

### Available Packs

| Pack | Description | Repo |
|------|-------------|------|
| **academic-journal** | Academic literature discovery, acquisition, and ingestion. Searches OpenAlex, Semantic Scholar, and Crossref for papers, resolves open access URLs, downloads PDFs, ingests them into the corpus, and builds a citation graph. Includes a 5-stage pipeline with background workers, rate limiting, and circuit breakers. | [marginalia-plugin-academic-journal](https://github.com/John-Cusack/marginalia-plugin-academic-journal) |
| **kindle** | Kindle Cloud Reader scraper. Extracts full book text from Amazon's Cloud Reader via Playwright browser automation. | [marginalia-plugin-kindle](https://github.com/John-Cusack/marginalia-plugin-kindle) |

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
  cli/              # CLI commands (serve, ingest, search, plugin, backup)
  domain/           # Core domain models and error types
  ports/            # Repository protocols
```

## Documentation

Full project documentation is in [corpus-engine-docs/](corpus-engine-docs/):

| Document | Purpose |
|----------|---------|
| [01-vision.md](corpus-engine-docs/docs/01-vision.md) | Product vision and principles |
| [02-prd.md](corpus-engine-docs/docs/02-prd.md) | Product requirements and scope |
| [03-architecture.md](corpus-engine-docs/docs/03-architecture.md) | System architecture and data flow |
| [04-data-model.md](corpus-engine-docs/docs/04-data-model.md) | Core schemas, entities, events |
| [05-mcp-spec.md](corpus-engine-docs/docs/05-mcp-spec.md) | MCP tool surface contract |
| [06-ingestion-modules.md](corpus-engine-docs/docs/06-ingestion-modules.md) | Ingestion module specification |
| [07-pack-system.md](corpus-engine-docs/docs/07-pack-system.md) | Pack manifest, SDK contract |
| [08-search-and-extraction.md](corpus-engine-docs/docs/08-search-and-extraction.md) | Hybrid search and extraction |
| [11-implementation-architecture.md](corpus-engine-docs/docs/11-implementation-architecture.md) | Implementation guide |

## License

MIT
