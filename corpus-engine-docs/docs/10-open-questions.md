# 10 — Open Questions & Decisions Needed

This document catalogs decisions the team needs to make that weren't
definitively resolved in the other specs. Each entry states the
question, the options, the tradeoffs, a recommendation, and who owns
the decision.

Most of these are not blocking for Phase 0 kickoff; those that are
are marked **BLOCKING**.

## OQ-1 — Embedding model choice **BLOCKING**

**Question:** Which embedding model should be the default for v1?

**Options:**

- **Local:** `BAAI/bge-large-en-v1.5` or `BAAI/bge-m3` via
  sentence-transformers. Pros: no API cost, no data leaves machine,
  good quality. Cons: requires GPU or patient CPU; machine setup is
  harder.
- **Hosted:** Anthropic embedding API (if/when available at reasonable
  cost), OpenAI `text-embedding-3-large`, Voyage `voyage-3`, Cohere
  `embed-v4`. Pros: no local compute, easy setup, consistent quality.
  Cons: recurring cost, data leaves machine, another API dependency.
- **Hybrid:** Default to local; auto-fallback to hosted if no GPU and
  user opts in.

**Tradeoffs:** The core privacy principle says no data leaves the
machine unnecessarily. Hosted embeddings violate that for every
passage ever ingested. Local embeddings impose real setup friction.

**Recommendation:** Default to `bge-m3` or similar strong local model,
with clear documentation on an optional hosted fallback. Provide a
setup check that warns if CPU-only embedding will be painfully slow
for the user's intended corpus size.

**Decision owner:** Tech lead, with PM input on privacy positioning.

**Must be decided by:** End of Phase 0.

## OQ-2 — Reranker choice

**Question:** Which cross-encoder reranker do we ship as default?

**Options:**

- Cohere Rerank (hosted, paid, very good quality, fast).
- BGE-reranker-large (local, free, slower).
- Voyage rerank (hosted, paid).
- Prompt-based rerank via Anthropic (convenient, higher latency,
  billable against main LLM budget).

**Recommendation:** Ship BGE-reranker-large as default (aligns with
local-first embedding choice), with plug-in support for hosted
rerankers via config. This lets power users opt into higher quality
without forcing the dependency.

**Decision owner:** Tech lead.

**Must be decided by:** Phase 1 Workstream 1.2.

## OQ-3 — LLM provider abstraction layer

**Question:** Do we build our own LLM abstraction or use an existing one
(LiteLLM, Instructor, LangChain LLMs)?

**Options:**

- **Roll our own.** Thin wrapper around the Anthropic SDK plus an
  OpenAI-compatible adapter. Total control; minimal deps.
- **LiteLLM.** Unified provider-agnostic interface; mature.
- **Instructor.** Adds structured output validation on top.
- **Anthropic SDK + Pydantic.** Use Anthropic native, lean on Pydantic
  for structured output.

**Recommendation:** Start with Anthropic SDK + Pydantic for the extraction
framework (native structured output is best-in-class). Abstract behind
a narrow `LLMClient` interface in `pack_sdk`. Add an OpenAI-compatible
adapter in Phase 2 if needed. Avoid LangChain.

**Decision owner:** Tech lead.

## OQ-4 — Pack code execution model

**Status: DECIDED. Staged rollout across v1 → v2 → v3+.**

**Question:** How is pack Python code executed?

### Option A — In-process, permission-declared (v1)

Pack code runs in the same Python process as the engine. The
`pack_sdk` exposes a narrow API; permissions are declared in the
manifest and the SDK surface is the enforcement boundary.

**Pros:**

- Zero performance overhead — tool calls are just function calls.
- Dead simple to implement; ships fast.
- Pack authors write normal Python with normal debugging, normal
  stack traces, normal imports. Low barrier to contribution.
- Shared state (DB connections, LLM client, embedding cache) is
  trivial.
- No serialization cost for passing passages, embeddings, or large
  result sets between core and pack.
- Native Python hot-reload works in dev mode.

**Cons:**

- A malicious or buggy pack can do anything the engine process can:
  read any file the user can read, make any network call, consume
  all memory, invoke `os.system`.
- Permission declarations are advisory — enforced only where the SDK
  mediates access. A pack that imports `requests` directly bypasses
  the "network: false" declaration.
- A crash in pack code crashes the engine.
- Python dependency conflicts between packs are unresolvable —
  everyone shares one `site-packages`.
- Users installing packs from random GitHub URLs are running
  arbitrary code; typo-squatting and compromised-maintainer attacks
  are plausible.

**When this is fine:** Single-user tool, user deliberately installs
packs they've evaluated, ecosystem is small and mostly trusted. This
matches the Obsidian / VS Code extension model.

### Option B — Subprocess per pack (v2)

Each pack runs in its own child Python process. Core talks to pack
processes over IPC (stdio pipes, Unix sockets, or local HTTP). The
OS enforces process boundaries; filesystem and network restrictions
applied via platform mechanisms (seccomp/landlock on Linux,
`sandbox-exec` on macOS).

**Pros:**

- Real isolation enforced by the OS.
- A pack crash doesn't take down the engine.
- Each pack can have its own Python environment — no shared-dependency
  hell.
- Permission declarations become enforceable: "network: false"
  becomes a kernel-level restriction.
- Resource limits (memory, CPU) enforceable per pack.
- Security posture is meaningfully stronger.

**Cons:**

- IPC serialization cost is real — passing a thousand passages
  across a process boundary for rerank is not free.
- Significantly more complex to build: process lifecycle, IPC
  protocol, error handling across boundaries, debugging experience.
- Pack authors now write code that runs in a constrained environment.
- Per-pack Python envs add disk, memory, and startup-time overhead.
- Shared resources (DB pool, LLM client) must be mediated through
  IPC — every DB query a pack runs becomes a request to core. That's
  the right security design, but it's work.
- Cross-platform sandbox primitives are uneven. Linux has good
  tools; macOS's `sandbox-exec` is deprecated; Windows is a separate
  project.

**When this becomes the right choice:** Once the pack ecosystem grows
past a trusted inner circle, or institutions want to deploy and their
security teams balk at in-process plugins, or commercial/closed-source
packs emerge whose authors the user doesn't fully trust.

### Option C — WASM (v3+)

Packs compile to WebAssembly (via Rust, AssemblyScript, Python-to-WASM,
etc.). Core runs them in a WASM runtime (wasmtime, wasmer).

**Pros:**

- Strongest isolation available; memory-safe by construction; no
  filesystem or network access except what the host explicitly
  provides via WASI.
- Cross-platform sandbox story is consistent — WASM runtimes behave
  the same on Linux, macOS, Windows.
- Permission model is precise and host-controlled.
- WASM modules are small and fast to load.
- Language-agnostic in theory (Rust, Go, TypeScript, anything that
  compiles to WASM).

**Cons:**

- Python doesn't compile to WASM cleanly. Python-in-WASM (Pyodide,
  RustPython) exists but is slow, has limited package availability,
  and dramatically increases the barrier to entry.
- The scholarly target audience is Python-comfortable, not
  Rust-comfortable. Requiring packs in a systems language kills
  the ecosystem.
- Host interactions (DB, LLM APIs, filesystem) require WASI-mediated
  bindings that must be designed and maintained.
- Debugging is meaningfully worse.
- Ecosystem tooling (testing, packaging, distribution) isn't mature
  for this use case.

**When this becomes the right choice:** Long-term, if the product
becomes institutional infrastructure with strict security
requirements and there's real engineering bandwidth to invest.

### Decision: staged rollout

- **v1 — Option A.** Ship it. Document the trust model explicitly in
  the pack install flow. This matches successful plugin ecosystems
  (Obsidian, VS Code, Neovim). Premature isolation kills ecosystems.
- **v2 — Option B for opt-in packs.** Keep Option A as default for
  performance; add opt-in subprocess execution for packs the user
  marks untrusted or that declare sensitive permissions. Security-
  conscious users get a real boundary without forcing overhead on
  everyone.
- **v3+ — Option C** if Corpus Engine becomes institutional
  infrastructure and the investment is justified.

**Design implication for the team:** the pack contract (manifest,
SDK, permission declarations) MUST NOT change between A and B.
Design the SDK *as if IPC might sit behind it* — pack authors must
not rely on shared in-memory state they shouldn't have. Done right,
migrating from A to B is an execution-model change rather than an
ecosystem-breaking API change.

**Decision owner:** Tech lead + PM (shared; security posture has
product implications).

## OQ-5 — How are schemas for `metadata` JSONB validated?

**Question:** Document-type metadata, entity-type attributes, etc. are
JSON Schema–declared by packs. Do we validate on write, on read, or
both?

**Options:**

- **Write-time only.** Fast reads; strict ingestion.
- **Read-time only.** Lenient ingestion; may surface bad data late.
- **Both.** Belt-and-suspenders; some cost.

**Recommendation:** Write-time validation with a `strict` flag
(default true). Support `--lenient` during ingestion to capture
non-conforming corpora with warnings. Read-time validation only for
data-integrity sweeps.

**Decision owner:** Tech lead.

## OQ-6 — Entity resolution approach

**Question:** How aggressively does the system auto-resolve entity
mentions vs. require manual curation?

**Options:**

- **Conservative.** Only exact alias matches auto-resolve; ambiguous
  cases go to a "pending" queue.
- **Assertive.** LLM judges disambiguation; stores confidence; user
  can override.
- **Tiered.** Exact → fuzzy → LLM, with confidence thresholds for each.

**Recommendation:** Tiered, with configurable confidence thresholds.
Default to conservative thresholds in Phase 1; tune as we see data.

**Decision owner:** Tech lead with primary user (who knows the data).

## OQ-7 — What's the "official" Anthropic connection story?

**Question:** What's the relationship, if any, between Corpus Engine
and Anthropic beyond "uses the API"?

**Options:**

- **Independent open-source project** that uses the Claude API.
- **Anthropic-published open source** under the Anthropic org.
- **Commercial product** built on Anthropic.

**Recommendation:** Independent open-source in v1. Keep optionality
open for later. Affects licensing (MIT or Apache 2.0 likely) and
naming.

**Decision owner:** PM / project lead.

## OQ-8 — What license does core ship under?

**Question:** MIT, Apache 2.0, AGPL, something else?

**Recommendation:** Apache 2.0 for core — permissive enough for
ecosystem, includes patent grant. Packs can choose their own licenses.

**Decision owner:** Project lead.

**Must be decided by:** Before first public commit.

## OQ-9 — Non-English language support

**Question:** How much non-English support in v1?

**Tradeoffs:** Hybrid search needs language-appropriate FTS
configuration (tsvector config per language); embeddings need to be
multilingual if the corpus is; rerankers need to cope.

**Recommendation:** v1 targets English primarily. `bge-m3` is
multilingual, so embedding support is fine. FTS uses
`pg_catalog.english` by default but can be configured per document.
Don't block on deep i18n; aim for "works acceptably if your corpus
has some non-English documents" rather than "equal quality across
all languages."

**Decision owner:** PM.

## OQ-10 — Attachment / image handling

**Question:** What does Corpus Engine do with images in PDFs or
attachments in EPUBs?

**Options:**

- **Ignore entirely (v1 plan).**
- **Extract and store as related files, not indexed.**
- **OCR any extracted images and add to index.**
- **Vision-LLM caption images and add captions to index.**

**Recommendation:** Ignore in v1. Attachments and image captions are
rich future work but not critical for the McClellan use case.

**Decision owner:** PM.

## OQ-11 — How do we handle corrections / annotations?

**Question:** When a user finds an extraction error or wants to
annotate a passage, what's the path?

**Options:**

- **Correction records** layered atop extractions (don't edit in place).
- **Destructive edits** with audit log.
- **Annotations as a separate layer** that extractions can respect.

**Recommendation:** Correction records — additive layer with "the
canonical answer is X, superseding extraction Y" semantics. Preserves
the original extraction for reproducibility; corrections carry
provenance too. User annotations are a parallel additive layer.

**Decision owner:** Tech lead + primary user.

## OQ-12 — Postgres version and distribution

**Question:** Do we bundle Postgres (Docker Compose) or require user
to install?

**Options:**

- **Bundle via Docker Compose.** One-command setup. Requires Docker.
- **Require local install.** More setup friction. No Docker dep.
- **Support both, document both.**

**Recommendation:** Support both, document both. Default install docs
use Docker Compose because it's reliably one-command. Power users and
Linux-natives can use local Postgres.

**Decision owner:** Tech lead.

## OQ-13 — Test corpus for CI

**Question:** The team needs a stable, legally clean test corpus for
CI and evaluation. What do we use?

**Options:**

- **Public domain founding-era documents** from Founders Online
  (public domain US government publication).
- **Project Gutenberg** texts.
- **Synthetic corpus** generated for tests.

**Recommendation:** Use Founders Online letters as the primary CI
fixture corpus — public domain, rich metadata, historically
interesting, similar enough in shape to the McClellan corpus that
tests exercise real behavior. Add Project Gutenberg material for
non-letter document types. Synthetic for pathological edge cases.

**Decision owner:** Tech lead.

**Must be decided by:** Phase 1 Workstream 1.1.

## OQ-14 — Bug reporting / telemetry

**Question:** Do we collect any telemetry?

**Recommendation:** No telemetry in v1. Privacy-first is core to the
product positioning. Bugs are reported via GitHub issues with users
volunteering logs.

**Decision owner:** PM.

## OQ-15 — Branding, naming, repo URL

**Status: Direction chosen (scholarly-annotation framing);
specific name still to be finalized.**

**Question:** What is the product called? Where does the repo live?

### Direction

The name should position the tool as a **personal library for
AI-assisted research** — reviving a scholarly practice (marginal
notes, commonplace books, concordances) rather than positioning as
a generic AI productivity tool. Avoid names that foreground AI
trendiness; foreground the research tradition AI is augmenting.

### Preferred lane: the "marginalia" family

The marginalia metaphor is the strongest semantic fit — it describes
exactly what the tool does (annotating, cross-referencing, tracing
ideas in the margins of one's own library) and signals a scholarly
rather than consumer-AI positioning.

### ⚠ Naming-collision finding

A pre-commit check on "Marginalia" and "MarginaliaAI" surfaced
significant prior art in the same space:

- **Marginalia Search** (marginalia.nu / github.com/MarginaliaSearch)
  — an established independent open-source internet search engine,
  active community, AGPL, existing MCP server integration. This is
  the most serious collision: overlapping audience (technical users,
  MCP ecosystem) and overlapping function vocabulary (search).
- **Marginalia** (Google Play) — a book-scanning app that already
  advertises "chat with your library using AI." Direct functional
  overlap.
- **jpmoo/marginalia** — an Obsidian plugin for AI-powered margin
  notes. Smaller but same functional space.

Using "Marginalia" or "MarginaliaAI" as-is would create real
user confusion, especially with MCP-ecosystem users who already
know `Marginalia Search`. This is a hard-pass recommendation on
the bare forms.

### Candidate names in the marginalia / scholarly-annotation lane

Listed with their positioning, pros, and cons. The team should
run full availability checks (domain, GitHub org, PyPI, trademark)
on the top 2–3 before committing.

- **Marginalia Research** — keeps the metaphor, explicitly differentiates
  from the search engine. Pro: clean positioning. Con: still risks
  confusion in text references ("Marginalia" gets truncated in
  conversation).
- **Glosa / Glossa** — a gloss is a scholar's interpretive note in the
  margin of a manuscript; "glossa" is the Latin root. Pro: historical,
  precise, shorter, likely cleaner availability. Con: "glossa" has some
  academic-journal prior art; check trademark carefully.
- **Scholia** — a scholium is a marginal commentary on a classical text;
  plural is scholia. Pro: exactly the right referent, distinctive,
  scholarly. Con: unfamiliar to many; pronunciation variation.
- **Commonplace** — the Renaissance/Enlightenment practice of keeping
  a personal indexed book of excerpts and ideas. Pro: strong conceptual
  fit, historically rich. Con: generic English word, SEO-hostile.
- **Commonplace.dev / commonplace.study** — the domain-as-disambiguator
  approach. Pro: solves the SEO problem cleanly. Con: name and domain
  have to be used together.
- **Apparatus** — in textual scholarship, the "critical apparatus" is
  the scholarly apparatus accompanying a text (variants, citations,
  commentary). Pro: precise scholarly meaning, distinctive. Con: also
  has general-tech connotations.
- **Provenance** — named after the anti-hallucination guarantee the
  product is built on. Pro: sharp, differentiated, describes
  distinctive value. Con: data-engineering prior art; may mislead.
- **Codex Research / Codexes** — codex evokes bound books and
  scholarship. Pro: evocative. Con: heavily overloaded in tech.

### Recommendation

Shortlist for availability checks:

1. **Glosa** (or **Glossa**) — best balance of scholarly fit,
   brevity, distinctiveness.
2. **Scholia** — strongest scholarly precision; slightly harder to
   communicate casually.
3. **Marginalia Research** — if the team is willing to always use
   the two-word form to disambiguate from the existing search engine.

Run trademark, domain (`.com`, `.dev`, `.study`, `.ai`), GitHub
organization, and PyPI checks on all three. Eliminate based on
hard collisions, then pick the most resonant survivor.

### Avoid

- Bare **"Marginalia"** and **"MarginaliaAI"** — established prior art
  in the same space with an existing MCP server.
- Any `-AI` suffix on a scholarly name — dates the product, dilutes
  the positioning the rest of the name is doing.
- **"Corpus Engine"** — working title only; too generic for a
  consumer-facing brand, and "corpus" is a term of art users shouldn't
  have to learn.

### Positioning language (once named)

Independent of the specific name, the product should be consistently
described as:

> *A personal library for AI-assisted research. Your own books,
> letters, and archives — deeply searchable, cross-referenced, and
> traceable to source.*

This is the line to sharpen and reuse across README, website, and
first-party pack descriptions.

**Decision owner:** PM / project lead.

**Must be decided by:** Before first public commit (not first public
release). The name affects the repo URL, package name, module
namespaces, and MCP tool prefixes — renaming later is expensive.

---

## How this document is used

- Phase 0 kickoff: resolve all **BLOCKING** items.
- Each subsequent phase: revisit and close any items whose milestone
  has arrived.
- Keep this document current. Items resolved go to
  `10-decisions-made.md` with a brief rationale for posterity.
