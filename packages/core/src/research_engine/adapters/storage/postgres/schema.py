"""SQLAlchemy Core table definitions for all core tables."""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import MetaData

metadata = MetaData(schema="core")


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
    sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
    sa.UniqueConstraint("content_hash", "source"),
)

sa.Index("documents_type_idx", documents.c.document_type)
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
# Containment lookups — "which node holds this passage" — probe by span within
# one document, which is the hot path joining the passage layer to the tree.
sa.Index(
    "document_nodes_span_idx",
    document_nodes.c.document_id,
    document_nodes.c.char_start,
    document_nodes.c.char_end,
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
    sa.Column("embedding", Vector(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("passage_id", "model", "model_version"),
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
# tsvector support, so we handle it via raw SQL in migrations and queries.

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

installed_packs = sa.Table(
    "installed_packs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("source_url", sa.Text, nullable=False),
    sa.Column("source_ref", sa.Text, nullable=False),
    sa.Column("installed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
    sa.Column("manifest", sa.JSON, nullable=False),
    sa.Column("permissions_granted", sa.JSON, nullable=False, server_default="{}"),
)
