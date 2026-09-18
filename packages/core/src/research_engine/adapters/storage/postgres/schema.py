"""SQLAlchemy Core table definitions for all core tables."""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import MetaData

metadata = MetaData(schema="core")

#: bge-m3's width. Migration 006 types the embedding column to it, and the
#: HNSW index below cannot exist without a dimensioned column.
EMBEDDING_DIM = 1024


class Ltree(sa.types.UserDefinedType):
    """Minimal `ltree` binding: correct DDL, values as plain strings.

    SQLAlchemy ships no ltree type. Subtree tests use the `<@` and `@>`
    operators through `sa.text`, so nothing here needs to model them — this
    exists so `metadata.create_all` emits `ltree` rather than `text`, and so the
    declared schema matches what migration 007 builds.
    """

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:  # noqa: ARG002
        return "ltree"

# --- Documents & Passages ---

documents = sa.Table(
    "documents",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("title", sa.Text),
    sa.Column("document_type", sa.Text, nullable=False),
    sa.Column("language", sa.Text),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("content_hash", sa.LargeBinary, nullable=False),
    sa.Column("parser", sa.Text, nullable=False),
    sa.Column("parser_version", sa.Text, nullable=False),
    sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("created_date_start", sa.DateTime(timezone=True)),
    sa.Column("created_date_end", sa.DateTime(timezone=True)),
    sa.Column("created_precision", sa.Text),
    sa.Column(
        "edition_id",
        sa.Uuid,
        sa.ForeignKey("bibliography.editions.id", ondelete="RESTRICT"),
    ),
    sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
    sa.UniqueConstraint("content_hash", "source"),
)

sa.Index("documents_type_idx", documents.c.document_type)
sa.Index("documents_edition_idx", documents.c.edition_id)
# NOTE: no GIN index on `metadata`. The column is `json`, not `jsonb`, and
# Postgres has no default GIN operator class for `json` — declaring one here made
# `metadata.create_all` fail outright, and migration 001 never created it, so the
# index was pure fiction. Metadata filtering casts to jsonb at query time (see
# `build_candidate_stmt`); indexing it properly means migrating the column type.

passages = sa.Table(
    "passages",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "document_id", sa.Uuid, sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    # Span in the document's canonical text. Nullable only until `reindex
    # chunks` has re-anchored passages written by the 1.0 chunkers.
    sa.Column("char_start", sa.Integer),
    sa.Column("char_end", sa.Integer),
    sa.Column("locator", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("token_count", sa.Integer),
    sa.Column("chunker", sa.Text, nullable=False),
    sa.Column("chunker_version", sa.Text, nullable=False),
    sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("content_hash", sa.LargeBinary, nullable=False),
    # The structural node this passage sits in. SET NULL, not CASCADE:
    # rebuilding a document's tree must not take its passages with it.
    sa.Column(
        "node_id", sa.Uuid,
        sa.ForeignKey("core.document_nodes.id", ondelete="SET NULL"),
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("document_id", "position", "chunker", "chunker_version"),
)

sa.Index("passages_document_idx", passages.c.document_id)
sa.Index("passages_node_idx", passages.c.node_id)
sa.Index("passages_doc_span_idx", passages.c.document_id, passages.c.char_start, passages.c.char_end)

# The canonical text a document's passage offsets address. Kept out of
# `documents` so search hydration does not pull a megabyte per row.
document_texts = sa.Table(
    "document_texts",
    metadata,
    sa.Column(
        "document_id", sa.Uuid, sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("normalized_text", sa.Text, nullable=False),
    sa.Column("normalization_version", sa.Text, nullable=False),
    sa.Column("parser", sa.Text, nullable=False),
    sa.Column("parser_version", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Quote verification searches the folded text, which is a substring match over
# whole documents; without this it is a sequential scan of every one of them.
sa.Index(
    "document_texts_norm_trgm",
    document_texts.c.normalized_text,
    postgresql_using="gin",
    postgresql_ops={"normalized_text": "gin_trgm_ops"},
)

# The document's structural tree: parts, chapters, sections as the author wrote
# them. Like passages, nodes are spans into `document_texts.text` and carry no
# prose of their own, so the tree survives re-chunking and costs only its
# skeleton. `path` is an ltree value — see domain/nodes.py for the label scheme.
document_nodes = sa.Table(
    "document_nodes",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "document_id", sa.Uuid, sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "parent_id", sa.Uuid, sa.ForeignKey("core.document_nodes.id", ondelete="CASCADE")
    ),
    sa.Column("path", Ltree(), nullable=False),
    sa.Column("depth", sa.Integer, nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("node_type", sa.Text, nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("char_start", sa.Integer, nullable=False),
    sa.Column("char_end", sa.Integer, nullable=False),
    sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.CheckConstraint("char_end >= char_start", name="document_nodes_span_ck"),
    sa.UniqueConstraint("document_id", "path"),
)

sa.Index("document_nodes_document_idx", document_nodes.c.document_id)
sa.Index("document_nodes_parent_idx", document_nodes.c.parent_id)
# Subtree tests use ltree's `<@`/`@>`, which need a GiST index to be anything
# but a scan.
sa.Index("document_nodes_path_gist", document_nodes.c.path, postgresql_using="gist")
# Containment lookups — "which node holds this passage" — probe by span within
# one document, which is the hot path joining the passage layer to the tree.
sa.Index(
    "document_nodes_span_idx",
    document_nodes.c.document_id,
    document_nodes.c.char_start,
    document_nodes.c.char_end,
)

# Every word of a source text that carries a morphological analysis, addressed
# against the same canonical string the passages are addressed against. This is
# the layer that makes a lexical question answerable: a pointed, inflected,
# prefixed Hebrew word has no searchable form — one lemma appears as hundreds of
# distinct strings — so "every occurrence of this word" cannot be asked of the
# text and has to be asked of its analysis.
#
# One row per word of the running text, never per morpheme: the row's span must
# quote its own word exactly, and that invariant is what lets the loader prove
# the index is complete by showing that no unclaimed stretch of the text holds a
# letter. `lemma` keeps the source's own compound value ("c/4941" — conjunction
# plus Strong's 4941); `strong` and `prefixes` are that value parsed, because a
# survey queries the number and reads the prefixes as findings.
words = sa.Table(
    "words",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column(
        "document_id",
        sa.Uuid,
        sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("char_start", sa.Integer, nullable=False),
    sa.Column("char_end", sa.Integer, nullable=False),
    sa.Column("surface", sa.Text, nullable=False),
    sa.Column("lemma", sa.Text, nullable=False),
    # Nullable on purpose: 5,966 words are a bare preposition or article whose
    # lemma is a morpheme letter with no Strong's number behind it at all.
    sa.Column("strong", sa.Text),
    # The homograph letter Strong's lacks — OSHB splits words Strong's merged,
    # so "834 a" and "834 b" are different words sharing a number.
    sa.Column("homograph", sa.Text),
    sa.Column("prefixes", sa.Text),
    sa.Column("morph", sa.Text, nullable=False),
    sa.Column("language", sa.Text, nullable=False),
    sa.Column("ref", sa.Text, nullable=False),
    sa.Column("from_qere", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.UniqueConstraint("document_id", "position"),
)

sa.Index("words_strong_idx", words.c.strong)
# A Strong's number is unique only inside its lexicon: H4941 and G4941 are
# different words. Every lookup by number must name a language, and this is the
# index that makes the correct query shape the fast one.
sa.Index("words_language_strong_idx", words.c.language, words.c.strong)
sa.Index("words_lemma_idx", words.c.lemma)
sa.Index("words_document_idx", words.c.document_id)
# Resolving a word to the verse that contains it is the same span lookup the
# passage layer makes, so it wants the same index shape.
#
# Measured 2026-09-08 at 305,517 rows, resolving all 422 occurrences of Strong's
# 4941 to their verse nodes: the span-containment join runs in 8.3 ms — an index
# scan here, then one `document_nodes_span_idx` probe per word at 0.012 ms.
# There is no foreign key to `document_nodes` and it is not missed at this size.
#
# `find_lemma` does not pay even that, because `words.ref` already carries the
# verse reference the ingest knew (1.4 ms for the same question). The
# containment join is the fallback for questions `ref` cannot answer — which
# node of some *other* edition a word sits in — and the number to re-measure
# before anything multiplies this table by an order of magnitude.
sa.Index("words_doc_span_idx", words.c.document_id, words.c.char_start, words.c.char_end)

# --- Versification: how editions disagree about where a verse sits ---
#
# Three editions of the same text in this corpus use three vocabularies for the
# same book and two traditions for the same verse number. Both are recorded as
# data rather than as rules, because both are already written down somewhere
# authoritative and a second implementation would be a second thing to be wrong.

editions_versification = sa.Table(
    "editions_versification",
    metadata,
    sa.Column("edition_key", sa.Text, primary_key=True),
    sa.Column("scheme", sa.Text, nullable=False),
    sa.Column("notes", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# `(edition, article code) -> OSIS book id`. LHB writes Ecclesiastes "ECC" and
# ESV writes it "EC"; a join on the raw code loses four Old Testament books and
# reports nothing wrong.
edition_books = sa.Table(
    "edition_books",
    metadata,
    sa.Column("edition_key", sa.Text, nullable=False),
    sa.Column("code", sa.Text, nullable=False),
    sa.Column("osis_id", sa.Text, nullable=False),
    sa.Column("name", sa.Text),
    # Canonical position, so "order by book" is a column rather than a hardcoded
    # list of sixty-six names somewhere in core.
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.PrimaryKeyConstraint("edition_key", "code"),
)

sa.Index(
    "edition_books_osis_idx",
    edition_books.c.edition_key,
    edition_books.c.osis_id,
    unique=True,
)

# A pair table, not an offset column: seven of the 1,978 WLC-KJV mappings are
# a verse beginning midway through another, which no integer offset expresses.
verse_map = sa.Table(
    "verse_map",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("from_scheme", sa.Text, nullable=False),
    sa.Column("to_scheme", sa.Text, nullable=False),
    sa.Column("from_ref", sa.Text, nullable=False),
    sa.Column("to_ref", sa.Text, nullable=False),
    sa.Column("from_part", sa.Text),
    sa.Column("to_part", sa.Text),
    sa.Column("mapping_type", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.CheckConstraint("mapping_type IN ('full', 'partial')", name="verse_map_type_known"),
    sa.CheckConstraint(
        "(from_part IS NULL AND to_part IS NULL) OR mapping_type = 'partial'",
        name="verse_map_parts_are_partial",
    ),
)

sa.Index(
    "verse_map_from_idx",
    verse_map.c.from_scheme,
    verse_map.c.to_scheme,
    verse_map.c.from_ref,
)
sa.Index(
    "verse_map_to_idx",
    verse_map.c.to_scheme,
    verse_map.c.from_scheme,
    verse_map.c.to_ref,
)

passage_embeddings = sa.Table(
    "passage_embeddings",
    metadata,
    sa.Column(
        "passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("model_version", sa.Text, nullable=False),
    sa.Column("dim", sa.Integer, nullable=False),
    # Dimensioned, because migration 006 types it and an HNSW index cannot be
    # built on a `vector` without a dimension. Declaring it bare said the column
    # was less constrained than it is, and left the index below undescribed.
    sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("passage_id", "model", "model_version"),
)

# Every semantic search depends on this and nothing described it. The index
# migration 006 built had been dropped by something that left no record, and it
# could not be noticed: `schema.py` never declared it, and the truthfulness test
# only asserted that declared indexes exist. Declared here so its absence is a
# failure rather than a slow query — see migration 017.
sa.Index(
    "passage_embeddings_hnsw",
    passage_embeddings.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
    postgresql_with={"m": 16, "ef_construction": 64},
)

passage_fts = sa.Table(
    "passage_fts",
    metadata,
    sa.Column(
        "passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("lang_config", sa.Text, nullable=False, server_default="english"),
    sa.Column("ts", sa.Text),  # tsvector - handled via raw SQL in migrations
)

# Note: passage_fts.ts is a tsvector column. SQLAlchemy Core doesn't have native
# tsvector support, so we handle it via raw SQL in migrations and queries. The
# GIN index is declarable even though the type is not — and it is the whole of
# keyword search, so it is worth saying so.
sa.Index("passage_fts_ts_idx", passage_fts.c.ts, postgresql_using="gin")

# --- Entities ---

entities = sa.Table(
    "entities",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("entity_type", sa.Text, nullable=False),
    sa.Column("canonical_name", sa.Text, nullable=False),
    sa.Column("disambiguator", sa.Text),
    sa.Column("attributes", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

sa.Index("entities_type_idx", entities.c.entity_type)
# Entity resolution matches names approximately, so the trigram index is what
# makes `resolve_entity` a lookup rather than a table scan.
sa.Index(
    "entities_name_trgm",
    entities.c.canonical_name,
    postgresql_using="gin",
    postgresql_ops={"canonical_name": "gin_trgm_ops"},
)
# No GIN index on this `json` column — Postgres has no default GIN operator
# class for `json` (only `jsonb`), so the declaration was unbuildable and the
# index never existed. See the note above `passages`.

entity_aliases = sa.Table(
    "entity_aliases",
    metadata,
    sa.Column(
        "entity_id", sa.Uuid, sa.ForeignKey("core.entities.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("alias", sa.Text, nullable=False),
    sa.Column("alias_type", sa.Text),
    sa.PrimaryKeyConstraint("entity_id", "alias"),
)

# The same approximate match as `entities_name_trgm`, over the names an entity
# is also known by.
sa.Index(
    "entity_aliases_alias_trgm",
    entity_aliases.c.alias,
    postgresql_using="gin",
    postgresql_ops={"alias": "gin_trgm_ops"},
)

# --- Mentions ---

mentions = sa.Table(
    "mentions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "entity_id", sa.Uuid, sa.ForeignKey("core.entities.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("span_start", sa.Integer),
    sa.Column("span_end", sa.Integer),
    sa.Column("surface_form", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

sa.Index("mentions_passage_idx", mentions.c.passage_id)
sa.Index("mentions_entity_idx", mentions.c.entity_id)

# --- Events ---

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("timestamp_start", sa.DateTime(timezone=True)),
    sa.Column("timestamp_end", sa.DateTime(timezone=True)),
    sa.Column("precision", sa.Text),
    sa.Column("location_id", sa.Uuid, sa.ForeignKey("core.entities.id")),
    sa.Column("location_text", sa.Text),
    sa.Column(
        "source_passage_id", sa.Uuid,
        sa.ForeignKey("core.passages.id", ondelete="SET NULL"),
    ),
    sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

sa.Index("events_type_idx", events.c.event_type)
# No GIN index on this `json` column — Postgres has no default GIN operator
# class for `json` (only `jsonb`), so the declaration was unbuildable and the
# index never existed. See the note above `passages`.

event_actors = sa.Table(
    "event_actors",
    metadata,
    sa.Column(
        "event_id", sa.Uuid, sa.ForeignKey("core.events.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "entity_id", sa.Uuid, sa.ForeignKey("core.entities.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("role", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint("event_id", "entity_id", "role"),
)

sa.Index("event_actors_entity_idx", event_actors.c.entity_id)

# --- Edges ---

edges = sa.Table(
    "edges",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("source_kind", sa.Text, nullable=False),
    sa.Column("source_id", sa.Uuid, nullable=False),
    sa.Column("target_kind", sa.Text, nullable=False),
    sa.Column("target_id", sa.Uuid, nullable=False),
    sa.Column("relation_type", sa.Text, nullable=False),
    sa.Column("attributes", sa.JSON, nullable=False, server_default="{}"),
    sa.Column(
        "source_passage_id", sa.Uuid,
        sa.ForeignKey("core.passages.id", ondelete="SET NULL"),
    ),
    sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

sa.Index("edges_source_idx", edges.c.source_kind, edges.c.source_id)
sa.Index("edges_target_idx", edges.c.target_kind, edges.c.target_id)
sa.Index("edges_relation_idx", edges.c.relation_type)
sa.Index(
    "edges_natural_key_uq",
    edges.c.source_kind,
    edges.c.source_id,
    edges.c.target_kind,
    edges.c.target_id,
    edges.c.relation_type,
    unique=True,
)

# --- Extraction Framework ---

extraction_schemas = sa.Table(
    "extraction_schemas",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("owner", sa.Text, nullable=False),
    sa.Column("schema", sa.JSON, nullable=False),
    sa.Column("prompt_template", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("name", "version", "owner"),
)

extractions = sa.Table(
    "extractions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "schema_id", sa.Uuid, sa.ForeignKey("core.extraction_schemas.id"),
        nullable=False,
    ),
    sa.Column("extractor_version", sa.Text, nullable=False),
    sa.Column("llm_model", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("records", sa.JSON, nullable=False, server_default="[]"),
    sa.Column("llm_call_id", sa.Uuid),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("passage_id", "schema_id", "extractor_version"),
)

sa.Index("extractions_schema_idx", extractions.c.schema_id)

extraction_records = sa.Table(
    "extraction_records",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "extraction_id", sa.Uuid, sa.ForeignKey("core.extractions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "schema_id", sa.Uuid, sa.ForeignKey("core.extraction_schemas.id"),
        nullable=False,
    ),
    sa.Column("record_type", sa.Text, nullable=False),
    sa.Column("data", sa.JSON, nullable=False),
    sa.Column("evidence_start", sa.Integer),
    sa.Column("evidence_end", sa.Integer),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

sa.Index("extraction_records_passage_idx", extraction_records.c.passage_id)
sa.Index("extraction_records_type_idx", extraction_records.c.record_type)
# No GIN index on this `json` column — Postgres has no default GIN operator
# class for `json` (only `jsonb`), so the declaration was unbuildable and the
# index never existed. See the note above `passages`.

# --- Provenance & Operations ---

llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("purpose", sa.Text, nullable=False),
    sa.Column("caller", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("input_tokens", sa.Integer),
    sa.Column("output_tokens", sa.Integer),
    sa.Column("cost_estimate", sa.Numeric(12, 6)),
    sa.Column("duration_ms", sa.Integer),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

ingestion_runs = sa.Table(
    "ingestion_runs",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("source_spec", sa.JSON, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("stats", sa.JSON, nullable=False, server_default="{}"),
)

ingestion_items = sa.Table(
    "ingestion_items",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "run_id", sa.Uuid, sa.ForeignKey("core.ingestion_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("source_ref", sa.Text, nullable=False),
    sa.Column("document_id", sa.Uuid, sa.ForeignKey("core.documents.id")),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("duration_ms", sa.Integer),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

sa.Index("ingestion_items_run_idx", ingestion_items.c.run_id)

plugin_activations = sa.Table(
    "plugin_activations",
    metadata,
    sa.Column("plugin_id", sa.Text, primary_key=True),
    sa.Column("distribution_name", sa.Text),
    sa.Column("distribution_version", sa.Text, nullable=False),
    sa.Column("entry_point_name", sa.Text),
    sa.Column("manifest_sha256", sa.Text),
    sa.Column("legacy_source_url", sa.Text),
    sa.Column("legacy_source_ref", sa.Text),
    sa.Column("installed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("state", sa.Text, nullable=False, server_default="legacy"),
    sa.Column("manifest", sa.JSON, nullable=False),
    sa.Column("permissions_granted", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("approved_at", sa.DateTime(timezone=True)),
    sa.Column(
        "approved_non_interactive",
        sa.Boolean,
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.Text),
    sa.Column("provenance", sa.JSON),
    sa.Column("database_revision", sa.Integer),
    sa.Column("database_status", sa.Text),
)

# --- Evidence: cited addresses ---

# One row per (document, coordinates). Declared with an explicit schema because
# the shared MetaData defaults to core. Mirrors 009_source_spans.
source_spans = sa.Table(
    "source_spans",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "document_id", sa.Uuid, sa.ForeignKey("core.documents.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("char_start", sa.Integer, nullable=False),
    sa.Column("char_end", sa.Integer, nullable=False),
    sa.Column("quoted_text", sa.Text, nullable=False),
    sa.Column("parser", sa.Text),
    sa.Column("parser_version", sa.Text),
    sa.Column(
        "passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="SET NULL")
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.CheckConstraint(
        "char_start >= 0 AND char_end > char_start", name="source_spans_range_ck"
    ),
    sa.UniqueConstraint(
        "document_id", "char_start", "char_end",
        name="source_spans_coordinates_uk",
    ),
    schema="evidence",
)

# --- Argument: the claim ledger ---

# Mirrors 010_argument. Anchors reference spans, never coordinates.
claims = sa.Table(
    "claims",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("ref", sa.Text, nullable=False, unique=True),
    sa.Column("statement", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="open"),
    sa.Column("confidence", sa.Float),
    sa.Column("steelman", sa.Text),
    sa.Column("public_ready", sa.Boolean, nullable=False, server_default="false"),
    sa.Column(
        "academic_candidate", sa.Boolean, nullable=False, server_default="false"
    ),
    sa.Column("attributes", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.CheckConstraint(
        "status IN "
        "('open','researching','rebutted','weakened','unresolved','conceded')",
        name="claims_status_ck",
    ),
    sa.CheckConstraint(
        "confidence IS NULL OR confidence BETWEEN 0 AND 1",
        name="claims_conf_ck",
    ),
    schema="argument",
)

claim_edges = sa.Table(
    "claim_edges",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "source_id", sa.Uuid, sa.ForeignKey("argument.claims.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "target_id", sa.Uuid, sa.ForeignKey("argument.claims.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("relation", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float),
    sa.Column("note", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("source_id", "target_id", "relation"),
    sa.CheckConstraint("source_id <> target_id", name="claim_edges_no_self"),
    schema="argument",
)

sa.Index("claim_edges_target_idx", claim_edges.c.target_id, claim_edges.c.relation)
sa.Index("claim_edges_source_idx", claim_edges.c.source_id, claim_edges.c.relation)

anchors = sa.Table(
    "anchors",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "claim_id", sa.Uuid, sa.ForeignKey("argument.claims.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column(
        "person_entity_id",
        sa.Uuid,
        sa.ForeignKey("core.entities.id", ondelete="SET NULL"),
    ),
    sa.Column(
        "source_span_id",
        sa.Uuid,
        sa.ForeignKey("evidence.source_spans.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("quoted_text", sa.Text, nullable=False),
    sa.Column("verify_status", sa.Text),
    sa.Column("verified_at", sa.DateTime(timezone=True)),
    sa.Column("parser_version", sa.Text),
    sa.Column(
        "edition_id",
        sa.Uuid,
        sa.ForeignKey("bibliography.editions.id", ondelete="RESTRICT"),
    ),
    sa.Column("edition_key", sa.Text),
    sa.Column("locator", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.CheckConstraint(
        "role IN ('asserts','supports','rebuts','context')",
        name="anchors_role_ck",
    ),
    sa.CheckConstraint(
        "verify_status IS NULL OR verify_status IN ('exact','normalized','near')",
        name="anchors_verify_ck",
    ),
    schema="argument",
)

sa.Index("anchors_claim_idx", anchors.c.claim_id)
sa.Index("anchors_span_idx", anchors.c.source_span_id)
sa.Index("anchors_edition_idx", anchors.c.edition_id)

# --- Bibliography: the editions stub ---

# Decision 12: one row per edition key, backfilled in 012, maintained at
# ingest. P3-1 extends it. Mirrors 012_authored_and_bibliography.
editions = sa.Table(
    "editions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("edition_key", sa.Text, nullable=False, unique=True),
    sa.Column("csl", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema="bibliography",
)

# --- Authored: works from their first freeze ---

works = sa.Table(
    "works",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("slug", sa.Text, nullable=False, unique=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("work_type", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="draft"),
    sa.Column("language", sa.Text),
    sa.Column("abstract", sa.Text),
    sa.Column("current_revision_id", sa.Uuid),
    sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("archived_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "status IN ('draft','review','published','archived')",
        name="works_status_ck",
    ),
    sa.ForeignKeyConstraint(
        ["current_revision_id", "id"],
        ["authored.work_revisions.id", "authored.work_revisions.work_id"],
        name="works_current_revision_fk",
        deferrable=True,
        initially="DEFERRED",
    ),
    schema="authored",
)

work_revisions = sa.Table(
    "work_revisions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "work_id", sa.Uuid, sa.ForeignKey("authored.works.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision_number", sa.Integer, nullable=False),
    sa.Column(
        "parent_revision_id",
        sa.Uuid,
        sa.ForeignKey("authored.work_revisions.id", ondelete="RESTRICT"),
    ),
    sa.Column("state", sa.Text, nullable=False, server_default="draft"),
    sa.Column("message", sa.Text),
    sa.Column("content_hash", sa.LargeBinary),
    sa.Column("created_by", sa.Text, nullable=False, server_default="user"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("frozen_at", sa.DateTime(timezone=True)),
    sa.Column("published_at", sa.DateTime(timezone=True)),
    sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
    sa.UniqueConstraint("id", "work_id"),
    sa.UniqueConstraint("work_id", "revision_number"),
    sa.CheckConstraint(
        "state IN ('draft','frozen','published','superseded')",
        name="work_revision_state_ck",
    ),
    schema="authored",
)

work_blocks = sa.Table(
    "work_blocks",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "revision_id",
        sa.Uuid,
        sa.ForeignKey("authored.work_revisions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("block_key", sa.Uuid, nullable=False),
    sa.Column("parent_id", sa.Uuid),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("block_type", sa.Text, nullable=False),
    sa.Column("title", sa.Text),
    sa.Column("body_markdown", sa.Text, nullable=False, server_default=""),
    sa.Column("attributes", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("id", "revision_id"),
    sa.UniqueConstraint("revision_id", "block_key"),
    sa.UniqueConstraint(
        "revision_id", "parent_id", "position",
        postgresql_nulls_not_distinct=True,
    ),
    sa.ForeignKeyConstraint(
        ["parent_id", "revision_id"],
        ["authored.work_blocks.id", "authored.work_blocks.revision_id"],
        ondelete="RESTRICT",
    ),
    schema="authored",
)

sa.Index(
    "work_blocks_revision_idx",
    work_blocks.c.revision_id,
    work_blocks.c.parent_id,
    work_blocks.c.position,
)

citation_occurrences = sa.Table(
    "citation_occurrences",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("citation_key", sa.Uuid, nullable=False),
    sa.Column(
        "block_id",
        sa.Uuid,
        sa.ForeignKey("authored.work_blocks.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("placement", sa.Text, nullable=False, server_default="inline"),
    sa.Column("intent", sa.Text, nullable=False, server_default="source"),
    sa.Column("note", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("block_id", "citation_key"),
    sa.CheckConstraint(
        "placement IN ('inline','block_end')", name="citation_placement_ck"
    ),
    sa.CheckConstraint(
        "intent IN ('source','support','contrast','background','definition',"
        "'translation','quotation','see_also')",
        name="citation_intent_ck",
    ),
    schema="authored",
)

citation_items = sa.Table(
    "citation_items",
    metadata,
    sa.Column(
        "occurrence_id",
        sa.Uuid,
        sa.ForeignKey("authored.citation_occurrences.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column(
        "edition_id",
        sa.Uuid,
        sa.ForeignKey("bibliography.editions.id", ondelete="RESTRICT"),
    ),
    sa.Column("edition_key", sa.Text),
    sa.Column(
        "source_span_id",
        sa.Uuid,
        sa.ForeignKey("evidence.source_spans.id", ondelete="RESTRICT"),
    ),
    sa.Column("quoted_text", sa.Text),
    sa.Column("verify_status", sa.Text),
    sa.Column("verified_at", sa.DateTime(timezone=True)),
    sa.Column("locator", sa.JSON, nullable=False, server_default="{}"),
    sa.Column("prefix", sa.Text),
    sa.Column("suffix", sa.Text),
    sa.Column("suppress_author", sa.Boolean, nullable=False, server_default="false"),
    sa.PrimaryKeyConstraint("occurrence_id", "position"),
    sa.CheckConstraint(
        "edition_id IS NOT NULL OR edition_key IS NOT NULL",
        name="citation_identity_ck",
    ),
    sa.CheckConstraint(
        "verify_status IS NULL OR verify_status IN ('exact','normalized','near')",
        name="citation_verify_ck",
    ),
    sa.CheckConstraint(
        "quoted_text IS NULL OR source_span_id IS NOT NULL",
        name="citation_quote_needs_span_ck",
    ),
    schema="authored",
)

sa.Index("citation_items_span_idx", citation_items.c.source_span_id)
sa.Index("citation_items_edition_idx", citation_items.c.edition_id)
sa.Index("citation_items_edition_key_idx", citation_items.c.edition_key)

block_source_links = sa.Table(
    "block_source_links",
    metadata,
    sa.Column(
        "block_id",
        sa.Uuid,
        sa.ForeignKey("authored.work_blocks.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "source_span_id",
        sa.Uuid,
        sa.ForeignKey("evidence.source_spans.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("relation", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float),
    sa.Column("note", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("block_id", "source_span_id", "relation"),
    sa.CheckConstraint(
        "confidence IS NULL OR confidence BETWEEN 0 AND 1",
        name="block_source_conf_ck",
    ),
    schema="authored",
)

sa.Index("block_source_links_span_idx", block_source_links.c.source_span_id)

block_entity_links = sa.Table(
    "block_entity_links",
    metadata,
    sa.Column(
        "block_id",
        sa.Uuid,
        sa.ForeignKey("authored.work_blocks.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "entity_id",
        sa.Uuid,
        sa.ForeignKey("core.entities.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("relation", sa.Text, nullable=False),
    sa.Column("surface_form", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("block_id", "entity_id", "relation"),
    schema="authored",
)

sa.Index(
    "block_entity_links_entity_idx",
    block_entity_links.c.entity_id,
    block_entity_links.c.relation,
)

waivers = sa.Table(
    "waivers",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "revision_id",
        sa.Uuid,
        sa.ForeignKey("authored.work_revisions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("rule_id", sa.Text, nullable=False),
    sa.Column("subject", sa.Text),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema="authored",
)

sa.Index("waivers_revision_idx", waivers.c.revision_id, waivers.c.rule_id)
