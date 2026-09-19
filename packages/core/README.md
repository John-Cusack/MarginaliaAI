# MarginaliaAI

**Give AI agents access to the books and papers you trust—not just what they
remember.**

Much of the information researchers rely on lives outside the open web: in
books, journals, archives, scans, research collections, and licensed databases.
MarginaliaAI turns sources you are authorized to use into a searchable, citable
corpus exposed through the Model Context Protocol (MCP). Agents can search the
actual sources, inspect relevant passages, and cite exact locations in the
canonical document.

The `marginalia-ai` distribution includes the CLI, PostgreSQL schema and
migrations, MCP server, remote inference clients, lightweight text ingestion,
extraction, entity and event services, and plugin host.

> **Developer preview:** MarginaliaAI requires PostgreSQL 15 or newer. Plugins
> are installed separately and must be explicitly audited and enabled.
> Integrations use accounts you are authorized to access; MarginaliaAI does not
> redistribute licensed source content.

The base distribution does not install PostgreSQL, database extensions, local
ML models, Docling, GPU drivers, or third-party plugins.

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

## Published plugins

Plugins are separate distributions installed into the same environment as
`marginalia-ai`. The currently published `0.6.x` plugin family is:

| Distribution | Plugin ID | Purpose |
|--------------|-----------|---------|
| [`marginalia-ai-plugin-history`](https://pypi.org/project/marginalia-ai-plugin-history/) `0.2.0` | `history` | Correspondence schemas and analysis tools |
| [`marginalia-ai-plugin-logos`](https://pypi.org/project/marginalia-ai-plugin-logos/) `0.2.0` | `logos` | Logos search, reference tools, and licensed-book ingestion |
| [`marginalia-ai-plugin-academic-journal`](https://pypi.org/project/marginalia-ai-plugin-academic-journal/) `0.2.0` | `academic-journal` | Scholarly discovery, acquisition, search, and citation graphs |
| [`marginalia-ai-plugin-yourcloudlibrary`](https://pypi.org/project/marginalia-ai-plugin-yourcloudlibrary/) `0.3.0` | `yourcloudlibrary` | Library catalog search and borrowed-book ingestion |

Kindle is not published on PyPI. Install any subset, or all published plugins:

```bash
python -m pip install \
  marginalia-ai-plugin-history \
  "marginalia-ai-plugin-logos[auth]" \
  marginalia-ai-plugin-academic-journal \
  marginalia-ai-plugin-yourcloudlibrary
# Needed only for Logos sign-in and YourCloudLibrary:
python -m playwright install chromium
```

Provider authentication is a separate, explicit step:

```bash
logos-login
research-engine-ycl-login
```

Installation makes static manifests discoverable but imports no plugin code.
Audit and enable the exact installed artifacts, migrate the two plugins that
own database tables, then restart the MCP server:

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

Enabled tools are advertised to MCP clients from each static manifest. Agents
should follow those tool descriptions instead of guessing parameters. See the
[complete plugin lifecycle and pipx instructions](https://github.com/John-Cusack/MarginaliaAI#published-plugins).

## Database

Use PostgreSQL 15 or newer with `vector`, `pg_trgm`, and `ltree` available. Creating extensions
may require an elevated database role. Set the async URL explicitly:

```bash
export RE_DB_URL='postgresql+asyncpg://user:password@localhost:5432/research_engine'
research-engine db upgrade
research-engine doctor
```

Runtime commands refuse an outdated schema and report the exact upgrade command;
they never migrate the database implicitly.

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
