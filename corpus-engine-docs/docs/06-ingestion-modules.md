# 06 — Ingestion Modules

This document specifies how Corpus Engine ingests documents from
different source types, which modules live in the core repository, and
which are intentionally kept out of the main repo. **Read §"Classification
policy" first.** It governs repository boundaries and is a deliberate
product decision, not an implementation detail.

## Classification policy

Every ingestion module falls into one of three categories.

### Category A — Core default

Modules in the main `corpus-engine` repository, enabled by default. These
handle unambiguously neutral formats: open standards, unencrypted files,
public archival formats. Shipping them creates no legal, ToS, ethical,
or reputational concerns.

### Category B — Official optional pack

Modules shipped as officially-maintained packs in separate repositories
under the project's organization (e.g. `corpus-engine-history`,
`corpus-engine-biblical`). These add domain-specific ingestion
(archival finding aids, OSIS biblical text, scientific article formats)
and are publicly advertised. Users install them deliberately, but they
are linked from the main docs and treated as first-party.

### Category C — Third-party / unofficial

Modules that handle content in a grey area — DRM-protected formats,
proprietary paid-platform content, scraped material from ToS-restricted
sites, or anything where the act of ingestion might violate a license,
Terms of Service, or technical protection measure.

**The main repository and official docs MUST NOT:**

- Ship code for Category C sources.
- Advertise, link to, recommend, or name specific Category C packs.
- Include how-to guidance for ingesting Category C material.
- Refer to Category C source brands by name in release notes, READMEs,
  or marketing material.

**The main repository MUST:**

- Architect the ingestion interface so that Category C packs, if users
  locate them independently, work cleanly.
- Remain silent on the subject of Category C; users who have such
  material already know what they have.

This policy protects the project from legal and reputational risk
without limiting what users can do with their own libraries. It is
modeled on the approach projects like Calibre take to DRM: the core
tool is clean; plugins that handle sensitive cases exist in the
community and users find them on their own.

## Module catalog

### Category A — Core default modules

| Module | Handles | Parser(s) | Chunker default | Notes |
|--------|---------|-----------|-----------------|-------|
| `pdf_text` | Text-extractable PDFs | pdfplumber / PyMuPDF | prose window | Reject if <50% text coverage; flag for external OCR. |
| `plain_text` | `.txt` | builtin | prose window | UTF-8; BOM detection. |
| `markdown` | `.md`, `.markdown` | mistletoe / markdown-it-py | structural (by heading) | Preserves headings as structural locators. |
| `epub` | Unencrypted EPUB 2/3 | ebooklib | structural (by spine item) | Refuses DRM-encrypted files with a clear error. |
| `html` | `.html`, web archives (.warc) | selectolax + readability-lxml | structural | Strip boilerplate; keep main content. |
| `tei_xml` | TEI P5 XML | lxml + TEI stylesheet | pack-configurable | Rich structural decomposition. |
| `generic_xml` | Well-formed XML | lxml | structural | Configurable XPath-based extraction. |
| `json_corpus` | JSON files containing arrays of documents | builtin | per-record | Each array element becomes one document. |
| `csv_metadata_sidecar` | CSV/YAML sidecars alongside other files | builtin | n/a | Attaches metadata to documents ingested by other modules. |

All Category A modules follow the common module contract (§"Module
contract" below).

### Category B — Official pack modules

Shipped in separate official repositories. Grouped by pack.

#### `corpus-engine-history`

| Module | Handles | Notes |
|--------|---------|-------|
| `ead_finding_aid` | EAD XML archival finding aids | Maps archival hierarchy to document structure. |
| `founders_online` | Founders Online API exports | National Archives' open founding-era corpus. |
| `loc_metadata` | Library of Congress metadata records | MODS/MARC XML. |
| `letters_edition` | Common published-letter-edition formats | e.g. Sears editions of McClellan letters when available as clean text. |

#### `corpus-engine-biblical`

| Module | Handles | Notes |
|--------|---------|-------|
| `osis_xml` | OSIS biblical text | Standard open format for biblical texts. |
| `usfm` | USFM biblical text | Common publishing format. |
| `theological_journal_jats` | JATS XML for theological journals | |

#### `corpus-engine-academic`

| Module | Handles | Notes |
|--------|---------|-------|
| `jats_article` | JATS XML (PubMed, open journals) | |
| `grobid_pdf` | GROBID-extracted structured PDFs | For academic papers. |
| `arxiv_export` | arXiv metadata + source | |

### Category C — Not in main repo, not linked from main docs

Listed here only to make explicit to the engineering team that the
architecture must accommodate them cleanly, **not** as a catalog to
build or advertise. Users who need these capabilities will locate
community packs on their own.

Shape of modules that would fall in Category C (generic descriptions,
no brand names in the main repo's docs):

- Modules that handle commercial ebook ecosystems with DRM.
- Modules that extract content from proprietary paid reference-software
  platforms.
- Modules that scrape content from sites whose ToS prohibits automated
  access.
- Modules that rely on user-provided decryption keys to access
  previously-purchased content.

Engineering requirements that support Category C without hosting it:

1. The ingestion module interface MUST be the same for A, B, and C
   packs. Category C packs are just normal packs that happen not to be
   in the official repos.
2. Category C packs MUST be installable by the standard
   `corpus-engine pack install <url>` mechanism.
3. The permissions model MUST let a Category C pack declare the access
   it needs (filesystem scope, local binaries it shells out to) and
   let the user approve it at install time.
4. Nothing in core MUST hardcode a prohibition or special case against
   any Category C source. The only distinction is distribution.

## Module contract

Every ingestion module, regardless of category, implements this contract.

```python
class IngestionModule:
    # Static metadata (usually from pack manifest)
    name: str                    # e.g. "pdf_text"
    version: str
    owner: str                   # "core" or "pack:<name>"
    supported_mime_types: list[str]
    supported_extensions: list[str]
    declared_permissions: PermissionSet

    def detect(self, source: SourceRef) -> DetectionResult:
        """Does this module handle this source? Returns confidence and reason."""

    def parse(self, source: SourceRef, config: dict) -> ParsedDocument:
        """
        Produce a ParsedDocument: canonical title/metadata + a sequence
        of structural units (sections, paragraphs, verses, etc.) with
        locators. No chunking yet; that's a separate step.
        """

    def default_chunker(self) -> str:
        """Name of the chunker best suited to this module's output."""

    def default_document_type(self) -> str:
        """Document type to register for produced documents."""

    def metadata_schema(self) -> JSONSchema:
        """JSON Schema for extended metadata this module produces."""
```

The core orchestrator handles:

- Dispatch (picking the right module via `detect`).
- Calling `parse`.
- Running the declared chunker on the parsed output.
- Embedding and indexing.
- Writing provenance.

Modules focus only on the format-specific work.

## Dispatch

Given an input source:

1. Check explicit declaration (user specified `--module pdf_text`).
2. Otherwise, run `detect` on all installed modules in priority order
   (core first, then packs by alphabetical order within category).
3. Pick the highest-confidence detector above threshold.
4. If no module claims the source, log a clear error and skip.

Users can override dispatch via config or per-batch flags.

## Metadata sidecars

For all modules, a sibling metadata file of the same basename is
automatically applied if present:

```
letters/
  mcclellan_1862_07_30.pdf
  mcclellan_1862_07_30.yaml   # <- applied automatically
```

Supported sidecar formats: `.yaml`, `.yml`, `.json`, `.toml`. Schema
varies by `document_type`; the extension pack declares it. A batch-level
metadata CSV is also supported.

Sidecar precedence: batch CSV → per-file sidecar → parser-extracted →
defaults. Later sources override earlier.

## Ingestion pipeline stages

```
┌─────────────┐
│  Discovery  │ Walk input paths, gather source refs.
└─────┬───────┘
      │
┌─────▼───────┐
│  Dispatch   │ Pick module via detect().
└─────┬───────┘
      │
┌─────▼───────┐
│   Parse     │ Module produces ParsedDocument.
└─────┬───────┘
      │
┌─────▼───────┐
│  Metadata   │ Apply sidecars; validate against document_type schema.
│  merge      │
└─────┬───────┘
      │
┌─────▼───────┐
│   Chunk     │ Run chunker (module default or override).
└─────┬───────┘
      │
┌─────▼───────┐
│   Embed     │ Compute embeddings in batches.
└─────┬───────┘
      │
┌─────▼───────┐
│   Write     │ Transaction: document + passages + fts + embeddings + provenance.
└─────┬───────┘
      │
┌─────▼───────┐
│  Post-hook  │ Optional pack post-hooks (entity extraction, event extraction, …).
└─────────────┘
```

Stages 3–7 happen in a single logical transaction per document:
either the whole document is committed or nothing about it is.

## Idempotency & re-ingestion

- Dedup on `(content_hash, source)`. Re-running on an unchanged source
  is a no-op.
- Re-running with a changed parser version re-parses and re-chunks;
  old passages are retired (soft-deleted with reason), not overwritten,
  so historical extractions still resolve.
- Metadata corrections never require re-parsing; they update the
  document row directly.

## Error handling

Per-file failures are captured in `ingestion_items` with:

- Source ref
- Module that attempted
- Error category (`parse_error`, `dispatch_miss`, `metadata_invalid`,
  `db_error`)
- Error message
- Retry-safe boolean

Batch completion reports summary counts and a machine-readable list of
failures.

## Performance expectations

- PDF (text-extractable): ≥100 pages/minute P50 on a modern laptop.
- Plain text / EPUB: limited by embedding throughput, not parsing.
- Embedding: batch requests; target ≥200 passages/minute with a
  local embedding model or a reasonable embedding API.
- Ingestion is checkpointable: Ctrl-C mid-batch leaves committed
  documents intact and lets the user resume with a `--resume` flag.

## Post-ingestion hooks (pack-provided)

Packs may register post-ingestion hooks that run automatically after
each document is committed:

- Entity extraction and linking.
- Event extraction (e.g. a `letter_sent` event for every ingested
  letter, derived from metadata).
- Specialized indexing (e.g. adding entries to pack-owned tables).

Hooks are declared in the pack manifest (see
[07-pack-system.md](07-pack-system.md)) and run synchronously within
the ingestion pipeline. Failing hooks log a warning but do not roll
back the document ingestion; the hook can be re-run later.

## User experience requirements

- Single command for common cases: `corpus-engine ingest ./letters`.
- Progress reporting with per-file status and ETA.
- Clear, actionable errors.
- `--dry-run` mode that reports what would be ingested by which
  module without writing.
- `--explain <file>` that shows how a specific file would be dispatched
  and parsed.

## Testing requirements

Every module ships with:

- A fixture corpus of at least 5 representative files.
- A golden-output test that parses the fixtures and asserts key
  properties (document count, passage structure, extracted metadata).
- A contract test asserting the module implements the `IngestionModule`
  interface correctly.

The core repository includes a shared fixture harness packs can reuse.
