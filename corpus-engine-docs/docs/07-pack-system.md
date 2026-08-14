# 07 — Pack System

This document specifies the pack architecture: how packs are structured,
how they're distributed and installed, what they can extend, and the
SDK contract between core and pack code.

## What a pack is

A pack is a self-contained directory (typically a git repository) that
extends Corpus Engine with domain-specific capabilities. A pack may
contribute any combination of:

- **Ingestion modules** — parsers for new source types.
- **Chunking strategies** — alternative chunking for specific document types.
- **Document types** — with JSON Schema for their extended metadata.
- **Entity types** — with typed attribute schemas.
- **Event types** — with payload schemas.
- **Relation types** — for the edges table.
- **Extraction schemas** — ready-to-use extraction templates.
- **MCP tools** — specialized tools composed of core primitives plus logic.
- **Post-ingestion hooks** — run after documents are committed.
- **Vocabularies / reference data** — entity sets, gazetteers, ontologies.

A pack may contribute any subset of these; a minimal "schema-only" pack
might contribute just one extraction schema and no code at all.

## Repository layout

```
<pack-repo>/
  pack.yaml                  # manifest — required
  README.md                  # human-facing description
  LICENSE
  CHANGELOG.md

  schemas/                   # JSON Schema files for types this pack defines
    document_types/
      letter.json
    entity_types/
      person.json
      military_unit.json
    event_types/
      letter_sent.json
      battle_occurred.json
    extraction_schemas/
      epistolary_references.yaml
      claims.yaml

  code/                      # pack Python code (if any)
    __init__.py
    ingestion/
      ead_finding_aid.py
    tools/
      find_missing_letters.py
      correspondence_cadence.py
    hooks/
      letter_sent_hook.py

  vocabularies/              # reference data
    civil_war_prosopography.csv
    us_gazetteer_1860.csv

  tests/
    fixtures/
      sample_ead/
      sample_letters/
    test_ingestion.py
    test_extractors.py

  docs/                      # optional extended pack docs
```

## Manifest (`pack.yaml`)

The manifest is the contract between the pack and core. Core validates
it on install and refuses incompatible packs.

```yaml
# Identity
name: history                          # unique within a user's installation
version: 0.3.1                         # semver
author: "John Smith <john@example.com>"
description: |
  Historical research pack: US Civil War period prosopography,
  epistolary reference extraction, and correspondence gap analysis.
license: MIT
homepage: https://github.com/johnsmith/corpus-engine-history

# Compatibility
requires:
  core_api: ">=1.0.0,<2.0.0"          # which core API versions this pack supports
  python: ">=3.11"
  packs: []                            # pack-level dependencies, if any

# Permissions the pack will need; user approves at install time
permissions:
  network: false                       # outbound HTTP
  llm: true                            # invokes LLM via core
  filesystem:
    read: ["corpus"]                   # corpus originals
    write: ["pack_data"]               # pack's private directory only
  subprocess: false                    # shells out to external binaries

# What this pack contributes
provides:
  document_types:
    - id: letter
      schema: schemas/document_types/letter.json
      default_chunker: whole_or_paragraph
      default_ingestion_module: pdf_text
      post_hooks:
        - history.hooks.letter_sent_hook:run

  entity_types:
    - id: person
      schema: schemas/entity_types/person.json
    - id: military_unit
      schema: schemas/entity_types/military_unit.json

  event_types:
    - id: letter_sent
      schema: schemas/event_types/letter_sent.json
    - id: battle_occurred
      schema: schemas/event_types/battle_occurred.json
    - id: claim_made
      schema: schemas/event_types/claim_made.json

  relation_types:
    - id: replies_to
    - id: encloses
    - id: references_letter

  ingestion_modules:
    - id: ead_finding_aid
      entry: history.ingestion.ead_finding_aid:EADModule
    - id: founders_online
      entry: history.ingestion.founders_online:FoundersOnlineModule

  extraction_schemas:
    - id: epistolary_references
      version: 2
      file: schemas/extraction_schemas/epistolary_references.yaml
    - id: claims
      version: 1
      file: schemas/extraction_schemas/claims.yaml

  mcp_tools:
    - id: find_missing_letters
      entry: history.tools.find_missing_letters:tool
      description: Find referenced-but-absent letters between two correspondents.
    - id: correspondence_cadence
      entry: history.tools.correspondence_cadence:tool
      description: Density timeline and anomaly flagging between correspondents.

  vocabularies:
    - id: civil_war_prosopography
      file: vocabularies/civil_war_prosopography.csv
      entity_type: person
      on_install: load                 # 'load' | 'available'
```

### Manifest validation rules

- `name` must be unique per user installation. Collisions are rejected.
- The pack's **top-level Python package name** (the first segment of every
  `entry`, e.g. `history` in `history.tools.find_missing_letters:tool`) must be
  globally unique across installed packs and must not shadow a Python
  standard-library module (so `code`, `email`, `json`, … are disallowed).
  Python caches imports globally by name; a collision would otherwise cause one
  pack to silently execute another's code. The loader raises a load error on
  violation. Convention: name the package after the pack (`acad`, `logos`,
  `history`).
- `core_api` compatibility must be satisfied or install is refused.
- All referenced schema files, entry points, and vocabulary files must
  resolve to existing files in the repo.
- All referenced entity/event/relation types must be defined in the
  same manifest's `provides` block (the pack may not depend on
  unpublished types from other packs without declaring the dependency).
- Permission declarations are authoritative: code that attempts
  undeclared access is denied by the core SDK with a clear error.

## Installation

### Installation sources

```bash
corpus-engine pack install github.com/user/pack-name
corpus-engine pack install github.com/user/pack-name@v0.3.1
corpus-engine pack install github.com/user/pack-name@main
corpus-engine pack install github.com/user/pack-name@<commit-sha>
corpus-engine pack install https://example.com/path/to/pack.git
corpus-engine pack install ./local-path/to/pack/     # dev install
```

### Install flow

1. Resolve source → git clone or local copy to a working directory.
2. Validate manifest.
3. Check core API compatibility.
4. Check pack dependencies (recursively install if allowed by config).
5. Display install summary:

   ```
   Installing: history 0.3.1
   From: github.com/johnsmith/corpus-engine-history@v0.3.1 (commit abc1234)

   This pack will:
     • Register 1 document type (letter)
     • Register 3 entity types (person, military_unit, battle)
     • Register 3 event types (letter_sent, battle_occurred, claim_made)
     • Register 2 ingestion modules (ead_finding_aid, founders_online)
     • Register 2 extraction schemas (epistolary_references, claims)
     • Register 2 MCP tools (find_missing_letters, correspondence_cadence)

   This pack requests the following permissions:
     • LLM calls (for extraction)
     • Read access: corpus originals
     • Write access: pack-private data directory

   Continue? [y/N]
   ```

6. On confirm: copy pack into installation directory
   (`~/.corpus-engine/packs/<name>@<version>/`), register in the
   `installed_packs` table, run any vocabulary `on_install: load` actions.
7. After install, the MCP server must be restarted before the pack's
   tools, types, and chunkers become available. The CLI prints a
   reminder after every install, uninstall, enable, and disable.

### Uninstall, enable, disable

```
corpus-engine pack uninstall <name>        # removes files and registry entries
corpus-engine pack disable <name>          # keeps files but unloads
corpus-engine pack enable <name>           # loads a disabled pack
corpus-engine pack list [--verbose]
corpus-engine pack audit <name>            # shows what pack registered + file tree
```

Uninstalling a pack does **not** delete data the pack created (extraction
records, entities, events) — those remain with their provenance intact.
If the pack is re-installed or another pack claims those types, data is
accessible again. A `--purge` flag can optionally remove pack-owned data.

### Restart contract

Any CLI command that changes which packs will load on next startup
(`install`, `uninstall`, `enable`, `disable`) requires the MCP server
to be restarted before the change takes effect. The catalogue is built
once at server startup; live reload is not yet supported.

## Core-pack boundary

The rule for what belongs in core vs. in a pack:

> **Would a librarian building a digital library of family recipes want
> this?** If yes, it's core. If only a specialist in one domain would
> want it, it's a pack.

Concrete applications:

| Concern | Core or Pack? |
|---------|---------------|
| Document/passage/entity/event tables | Core |
| Hybrid search | Core |
| Extraction framework (mechanism) | Core |
| Provenance tracking | Core |
| PDF ingestion | Core |
| EPUB ingestion | Core |
| EAD finding-aid ingestion | Pack (history) |
| OSIS biblical text ingestion | Pack (biblical) |
| Entity type "person" | Core |
| Entity type "military_unit" | Pack (history) |
| Entity type "verse" | Pack (biblical) |
| Event type "letter_sent" | Pack (history) |
| Event type "claim_made" | Debatable; likely core |
| Correspondence cadence analysis | Pack (history) |
| Pericope view | Pack (biblical) |
| Sentiment analysis | Pack (or user-defined schema) |

When in doubt, put it in a pack. Extracting a utility from a pack into
core later is easy. Retrofitting a too-specific abstraction out of core
is painful.

## The pack SDK

Packs do not import from core internals. They import from a stable SDK
module:

```python
from corpus_engine.pack_sdk import (
    # Types
    Document, Passage, Entity, Event, Edge,
    ExtractionSchema, ExtractionRecord,
    SourceRef, ParsedDocument, DetectionResult,

    # Base classes for contributions
    IngestionModule, Chunker, MCPTool, PostIngestionHook,

    # Services available to pack code (scoped by declared permissions)
    CorpusClient,          # search, get_document, etc.
    ExtractionClient,      # run extractions
    EntityClient,          # create/resolve entities
    EventClient,           # create events
    LLMClient,             # invoke LLM (only if permission granted)
    Logger,

    # Decorators
    tool,                  # marks a function as an MCP tool
    hook,                  # marks a function as a post-ingestion hook
)
```

Everything reachable from `pack_sdk` is versioned and stable within a
major core API version. Everything else is internal and subject to
change.

## Example pack contributions

### Schema-only pack (no Python code)

A pack contributing only an extraction schema. `pack.yaml`:

```yaml
name: simple-sentiment
version: 0.1.0
author: "…"
description: Generic sentiment extraction for any corpus.
requires:
  core_api: ">=1.0.0,<2.0.0"
permissions:
  llm: true
provides:
  extraction_schemas:
    - id: generic_sentiment
      version: 1
      file: schemas/extraction_schemas/generic_sentiment.yaml
```

That's a complete pack. Install it and `generic_sentiment:v1` is
available as an extraction schema to any agent.

### MCP tool pack (code + schema)

```python
# code/tools/find_missing_letters.py
from corpus_engine.pack_sdk import tool, CorpusClient, ExtractionClient

@tool(
    id="find_missing_letters",
    description="Find referenced-but-absent letters between two correspondents.",
    input_schema={
        "type": "object",
        "properties": {
            "correspondent_a_entity_id": {"type": "string"},
            "correspondent_b_entity_id": {"type": "string"},
            "date_range": {"type": "object"},
            "method": {"enum": ["referenced", "cadence", "content_inference", "all"]},
            "min_confidence": {"type": "number"},
        },
        "required": ["correspondent_a_entity_id", "correspondent_b_entity_id"],
    },
)
def find_missing_letters(corpus: CorpusClient, extraction: ExtractionClient, **args):
    # 1. Find letters between the two correspondents in range.
    # 2. Run or fetch epistolary_references extraction against those letters.
    # 3. For each extracted reference, try to resolve to a known letter.
    # 4. Collect unresolved references as missing-letter candidates.
    # 5. Return structured results.
    ...
```

### Ingestion module

```python
# code/ingestion/ead_finding_aid.py
from corpus_engine.pack_sdk import IngestionModule, SourceRef, ParsedDocument

class EADModule(IngestionModule):
    name = "ead_finding_aid"
    version = "0.3.1"
    supported_extensions = [".xml"]
    supported_mime_types = ["application/xml"]

    def detect(self, source: SourceRef):
        # sniff namespace / root tag
        ...

    def parse(self, source: SourceRef, config):
        # produce ParsedDocument with archival hierarchy
        ...

    def default_chunker(self):
        return "structural"

    def default_document_type(self):
        return "archival_finding_aid"

    def metadata_schema(self):
        return { ... }
```

## Versioning & compatibility

- **Semantic versioning** for packs (`MAJOR.MINOR.PATCH`).
- **Core API version** is declared in the manifest. Major bumps to core
  API mean packs must be updated; core refuses to load packs targeting
  an incompatible major version.
- **Extraction schema versioning** is independent and explicit: schemas
  have integer versions that appear in extraction IDs. Raising a schema
  version always produces a new generation of extractions rather than
  overwriting; users can compare.

## Trust & security

### Install-time

- User sees the full summary of what the pack provides and what
  permissions it requests.
- User explicitly confirms install.
- Manifest and repo URL + commit SHA are recorded in `installed_packs`.

### Runtime

- Pack code only has access to the `pack_sdk` surface. Clients are
  scoped to declared permissions; undeclared access raises
  `PermissionError`.
- Pack code executes in the main process in v1 (see §"Sandboxing
  roadmap").

### Sandboxing roadmap

v1 relies on the permission declaration + user trust model; pack code
runs in-process. This is adequate for a single-user tool where the
user is actively choosing what to install.

v2 considerations:

- Subprocess isolation per pack (Python `multiprocessing` with
  restricted interpreters).
- Seatbelt/landlock-style filesystem restriction on pack processes.
- WASM-based pack execution for strict isolation (longer-term).

### Verified / official packs

- The core repository maintains a hardcoded list of "official" pack
  identifiers. Installing an official pack from the expected URL shows
  a verified badge in CLI output.
- "Community verified" is a future possible tier; not in v1.

## Developer experience for pack authors

### Scaffolding

```
corpus-engine pack init my-new-pack
```

Creates a working pack skeleton with:

- A valid `pack.yaml`
- One trivial extraction schema
- A sample MCP tool
- A fixtures directory
- A test harness
- A README template with a fill-in-the-blanks checklist

Install it locally (`corpus-engine pack install ./my-new-pack`) and
iterate.

### Reloading in development

```
corpus-engine pack reload <name>
```

Re-reads a dev-installed pack without reinstalling. For manifest
changes, a full `pack install` is still needed.

### Testing

```
corpus-engine pack test <path>
```

Runs the pack's test suite against an ephemeral test library. Pack
tests use fixtures the pack ships with; the harness is provided by
`corpus_engine.pack_sdk.testing`.

### Publishing

Since distribution is GitHub-URL-based:

1. Commit to a repo.
2. Tag a release (`v0.3.1`).
3. That's publication. Users install with
   `corpus-engine pack install <repo>@v0.3.1`.

No central submission or approval step.

## Future: a lightweight registry

Not in v1, but architected-for:

- A public git repo containing a list of known packs with metadata
  pulled from their manifests.
- `corpus-engine pack search <query>` queries this registry.
- PR-based submissions. No dedicated infrastructure needed.
- Later, a simple static web UI atop the registry repo.

## Non-goals of the pack system (v1)

- No multi-language pack support. Python only in v1.
- No automatic dependency resolution with complex constraints. Linear
  ranges only; fail loudly on conflicts.
- No per-pack virtual environments. Packs share the engine's Python
  environment; conflicting deps are a known limitation addressed by
  dependency discipline in official packs and documentation for
  third-party pack authors.
