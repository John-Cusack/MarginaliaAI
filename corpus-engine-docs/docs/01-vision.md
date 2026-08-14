# 01 — Product Vision

## One-line statement

**Corpus Engine turns a personal library into a research superpower by making
every document deeply queryable, extractable, and cross-linkable through an
AI agent.**

## The problem

Researchers, historians, theologians, scientists, lawyers, and serious
hobbyists accumulate large personal libraries of documents: books, articles,
letters, archival scans, transcripts, and domain-specific files (Logos
resources, Kindle books, legal briefs, lab notebooks). These libraries are
effectively read-only — a human can read one book at a time, maybe search a
few titles for a keyword. The full corpus is never treated as a single,
queryable body of knowledge.

Meanwhile, AI has made it possible to:

- Search semantically across thousands of documents in milliseconds.
- Extract structured claims, references, and relationships from prose.
- Cross-link material across works the way Logos Bible Software cross-links
  verses — but for *any* domain, not just biblical studies.
- Build reusable research workflows that re-run as corpora grow.

What's missing is the plumbing: a general-purpose engine that ingests
heterogeneous sources, indexes them with rich metadata and embeddings, and
exposes this capability to an AI agent through a well-designed tool
interface. That's Corpus Engine.

## Who this is for

**Primary users** (Phase 1–2):

- **Serious researchers** (academic and independent) working across large
  document collections — historians, theologians, literary scholars.
- **Subject-matter practitioners** who want to leverage their own accumulated
  libraries — lawyers, policy analysts, medical researchers, investors.
- **Technically confident power users** comfortable running a local tool,
  installing packs, and working in Claude Code or a similar agent.

**Secondary users** (Phase 3+):

- **Domain communities** who maintain packs for their field (e.g. a biblical
  studies community pack, a Civil War pack, a genomics pack).
- **Institutions** (archives, libraries, small research centers) that want
  to expose their holdings to researchers through a standard MCP interface.

**Not for** (at least not directly in v1):

- Casual consumers looking for a better reading app.
- Enterprise customers needing centralized multi-user deployment.
- Users unwilling to run local software or connect it to an LLM.

## The guiding example

**The McClellan/Barlow research problem** is our reference use case and
should anchor product decisions:

> A historian has 847 letters from George B. McClellan's correspondence. They
> want to understand McClellan's evolving beliefs about Lincoln during 1862,
> identify missing letters in his correspondence with S.L.M. Barlow, and
> build a timeline of his private stance alongside public military events.

The system must make this kind of work *dramatically* faster than reading
through the letters by hand, while never losing traceability back to the
primary source. If Corpus Engine can't make this use case feel magical,
nothing else matters.

## Vision principles

These principles decide close calls throughout the project.

### 1. The corpus is sacred; derivations are disposable

Raw documents and their provenance are immutable and authoritative.
Extractions, embeddings, entity resolutions, and claims are all
*derivations* that can be recomputed. Never let derivation logic corrupt or
constrain the source layer.

### 2. Every derived fact cites its source span

No claim, timeline entry, entity mention, or extracted relation exists
without a pointer back to the exact passage and byte range that generated
it. This is non-negotiable. It is what makes the system trustworthy for
real research.

### 3. Hybrid search is the core superpower

Vector search + keyword search + metadata filters + reranking, unified
behind a single search primitive. Everything else — timelines, gap
detection, claim tracking — builds on this foundation.

### 4. Time and uncertainty are first-class

Fuzzy dates ("summer 1862"), conjectural authorship, disputed facts, and
varying precision are represented honestly in the data model, not forced
into false certainty.

### 5. The core is domain-agnostic; packs carry domain knowledge

The core engine knows about documents, passages, entities, events,
relationships, and search. It knows nothing about letters, verses, genes,
or case citations. Domain-specific knowledge lives in packs.

### 6. Packs are GitHub repos, installed by URL

No central registry required. Users install packs by pointing at a git
repo. Capabilities are declared in a manifest. The ecosystem grows without
gatekeeping.

### 7. The product is an MCP server, not an app

Corpus Engine is a backend that exposes a well-designed tool surface over
MCP. Claude Code (or Cursor, or any MCP-capable agent) is the interface.
The "app" is the agent loop, which users already have.

### 8. Ingestion is iterative and tolerant

Documents enter the library with whatever metadata is available. Later
passes enrich them. A lossy OCR'd scan is still a valid corpus member —
it just gets flagged as low-quality.

### 9. Some sources are sensitive

Certain source types (DRM-protected ebooks, paid research platforms,
proprietary software formats) require ingestion approaches that shouldn't
be advertised in the main project repo. The architecture supports
private/unofficial packs that users install deliberately. The official
repo ships only clean, uncontroversial defaults.

### 10. Build by extraction, not by anticipation

Architectural abstractions should be extracted from at least two working
concrete implementations. Anticipatory framework design is banned. The
McClellan work drives Phase 1; the second domain forces the right
abstractions.

## What success looks like

### Near-term (Phase 1 — ~3–4 months)

- A single researcher (the primary author) uses Corpus Engine daily for
  real historical research on McClellan's correspondence.
- Claude Code, connected to the engine over MCP, can hybrid-search the
  corpus, extract structured claims, build timelines, and surface gap
  candidates — all on real data.
- Research work produced with the tool is attributable to primary sources
  with zero hallucinated citations.

### Mid-term (Phase 2 — ~6–9 months)

- A second domain (biblical studies, leveraging prior Logos work) is
  supported as a separate pack.
- The core/pack separation is cleanly factored. Writing a third pack from
  scratch takes days, not weeks.
- An external user can install the core and one pack from GitHub and be
  productive within an afternoon.

### Long-term (Phase 3+ — 12+ months)

- A small community of pack authors publishes domain packs for fields the
  maintainers don't specialize in.
- Institutions begin exposing holdings via compatible ingestion adapters.
- The system demonstrably surfaces research leads (missing documents,
  contradictions, evolving positions) that traditional manual research
  would miss or take weeks to assemble.

## What this is not

- Not a reading app. Corpus Engine has no UI for leisurely reading;
  reading apps exist.
- Not a citation manager. Zotero and Obsidian exist; we integrate rather
  than replace.
- Not an LLM. The engine calls whatever LLM the user has configured for
  extraction; it doesn't train or serve its own model.
- Not a replacement for archives. The system reveals what's missing from
  a corpus; it doesn't digitize new material itself (though it may
  generate demand for exactly that kind of work).

## The theory of change

The bet behind Corpus Engine is that the bottleneck in serious research
has quietly shifted. Twenty years ago, the bottleneck was *access* —
finding the document. Today, for anyone with access to a decent library
and archival photos, the bottleneck is *synthesis* — reading, cross-
referencing, noticing patterns, detecting gaps, building timelines across
thousands of documents.

LLMs can do this synthesis at machine speed if and only if they have
disciplined access to structured, citation-grounded corpus data.
Corpus Engine is the infrastructure that gives them that access. If it
works, a single researcher with a laptop can do the work of a small
research team, and the depth achievable by serious scholars grows by an
order of magnitude.
