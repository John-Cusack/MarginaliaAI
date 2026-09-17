# Research Engine

Research Engine builds a searchable, citable research corpus and exposes it to MCP clients.
The base package includes the CLI, PostgreSQL schema/migrations, MCP server, remote inference
clients, lightweight text ingestion, extraction, entity/event services, and plugin host.

It does not install PostgreSQL, database extensions, local ML models, Docling, GPU drivers, or
third-party plugins.

## Install

```bash
python -m pip install marginalia-ai
# Everything, including local inference and document AI:
python -m pip install "marginalia-ai[full]"
```

Optional features are independently installable:

- `marginalia-ai[openai]` — OpenAI-compatible LLM adapter;
- `marginalia-ai[local-inference]` — sentence-transformers embedding and reranking;
- `marginalia-ai[documents]` — PDF text, EPUB, HTML, and TEI parsers;
- `marginalia-ai[document-ai]` — Docling layout/OCR and office/image conversion;
- `marginalia-ai[embed-server]` — FastAPI/Uvicorn plus its local inference runtime.

Local inference and Docling may download multi-gigabyte models and can require substantial disk,
RAM, and GPU capacity. A standard PyPI install does not select PyTorch's alternate CPU wheel
index; follow PyTorch's CPU installation instructions first when required.

## Database

Use PostgreSQL 15 or newer with `vector`, `pg_trgm`, and `ltree` available. Creating extensions
may require an elevated database role. Set the async URL explicitly:

```bash
export RE_DB_URL='postgresql+asyncpg://user:password@localhost:5432/research_engine'
ALEMBIC_INI="$(python -c 'from importlib.resources import files; print(files("research_engine").joinpath("adapters/storage/postgres/migrations/alembic.ini"))')"
alembic -c "$ALEMBIC_INI" upgrade head
research-engine doctor
```

`pg_dump` and `pg_restore` are external requirements for backup commands.

## Run over MCP

```json
{
  "mcpServers": {
    "research-engine": {
      "type": "stdio",
      "command": "research-engine",
      "args": ["serve"]
    }
  }
}
```

No checkout or repository working directory is required. `research-engine --help` and
`research-engine config --help` describe the installed command surface.

## Data and trust

Remote LLM providers receive prompts and selected corpus text and may charge per token. Review
provider settings and budgets before ingestion or extraction.

Plugins are ordinary Python distributions. Installation makes their static manifests available;
`research-engine plugin enable ID` displays and records the exact version, hash, contributions,
and permissions before code is imported. Enabled plugins execute in-process. Scoped clients are
the supported API boundary, not a security sandbox; enable only trusted artifacts.

See the [documentation](https://github.com/John-Cusack/MarginaliaAI/tree/main/docs),
[changelog](https://github.com/John-Cusack/MarginaliaAI/blob/main/CHANGELOG.md),
[issues](https://github.com/John-Cusack/MarginaliaAI/issues), and
[Apache-2.0 license](https://github.com/John-Cusack/MarginaliaAI/blob/main/LICENSE).
