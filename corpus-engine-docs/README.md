# Corpus Engine — Project Documentation

This directory contains the full specification package for **Corpus Engine**, a
general-purpose research corpus engine designed to make private libraries
deeply queryable through Claude Code and other MCP-capable agents.

## Documentation Index

Read in the order below for a top-down understanding.

| # | Document | Audience | Purpose |
|---|----------|----------|---------|
| 1 | [01-vision.md](docs/01-vision.md) | Everyone | Product vision, problem, users, principles |
| 2 | [02-prd.md](docs/02-prd.md) | PM, Eng leads | Product requirements, scope, non-goals, success criteria |
| 3 | [03-architecture.md](docs/03-architecture.md) | Engineers | System architecture, components, data flow |
| 4 | [04-data-model.md](docs/04-data-model.md) | Engineers | Core schemas, entities, events, relationships |
| 5 | [05-mcp-spec.md](docs/05-mcp-spec.md) | Engineers, pack authors | MCP tool surface contract |
| 6 | [06-ingestion-modules.md](docs/06-ingestion-modules.md) | Engineers | Ingestion module spec, source types, which belong in core vs. packs |
| 7 | [07-pack-system.md](docs/07-pack-system.md) | Engineers, pack authors | Pack manifest, distribution, SDK contract |
| 8 | [08-search-and-extraction.md](docs/08-search-and-extraction.md) | Engineers | Hybrid search and extraction framework |
| 9 | [09-roadmap.md](docs/09-roadmap.md) | PM, Eng leads | Phased build plan, milestones, sequencing |
| 10 | [10-open-questions.md](docs/10-open-questions.md) | Everyone | Unresolved decisions flagged for the team |
| 11 | [11-implementation-architecture.md](docs/11-implementation-architecture.md) | Senior engineers | Concrete implementation guide: code layout, wiring, plugin mechanics, extension patterns |

## How this package is meant to be used

1. The PM / project lead reads `01` and `02` to confirm intent.
2. Engineering leads read `03` through `08` to plan implementation.
3. The team uses `09` to sequence work and `10` to surface decisions that
   need to be made before or during Phase 1.
4. Individual specs (`04`–`08`) are the authoritative reference once coding
   begins; anything unclear gets raised and the spec is updated.

## Conventions

- **Core** — the general-purpose engine; knows nothing about specific domains.
- **Pack** — a domain extension (history, biblical studies, biology, …)
  distributed as a GitHub repo.
- **Module** — an ingestion adapter for a specific source type (PDF, EPUB,
  Logos, Kindle, …). Some live in core; some live in packs.
- **MCP** — Model Context Protocol. The engine exposes its capabilities to
  Claude Code and similar agents over MCP.
