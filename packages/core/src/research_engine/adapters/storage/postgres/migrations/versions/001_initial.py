"""Initial schema creation.

Revision ID: 001_initial
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create schema
    op.execute("CREATE SCHEMA IF NOT EXISTS core")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Documents
    op.create_table(
        "documents",
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
        schema="core",
    )
    op.create_index("documents_type_idx", "documents", ["document_type"], schema="core")

    # Passages
    op.create_table(
        "passages",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("document_id", sa.Uuid, sa.ForeignKey("core.documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("locator", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer),
        sa.Column("chunker", sa.Text, nullable=False),
        sa.Column("chunker_version", sa.Text, nullable=False),
        sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "position", "chunker", "chunker_version"),
        schema="core",
    )
    op.create_index("passages_document_idx", "passages", ["document_id"], schema="core")

    # Passage embeddings
    op.execute("""
        CREATE TABLE core.passage_embeddings (
            passage_id uuid NOT NULL REFERENCES core.passages(id) ON DELETE CASCADE,
            model text NOT NULL,
            model_version text NOT NULL,
            dim int NOT NULL,
            embedding vector NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (passage_id, model, model_version)
        )
    """)

    # Passage FTS
    op.execute("""
        CREATE TABLE core.passage_fts (
            passage_id uuid PRIMARY KEY REFERENCES core.passages(id) ON DELETE CASCADE,
            lang_config regconfig NOT NULL DEFAULT 'english',
            ts tsvector NOT NULL
        )
    """)
    op.execute("CREATE INDEX passage_fts_ts_idx ON core.passage_fts USING gin (ts)")

    # Entities
    op.create_table(
        "entities",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("canonical_name", sa.Text, nullable=False),
        sa.Column("disambiguator", sa.Text),
        sa.Column("attributes", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="core",
    )
    op.create_index("entities_type_idx", "entities", ["entity_type"], schema="core")
    op.execute("CREATE INDEX entities_name_trgm ON core.entities USING gin (canonical_name gin_trgm_ops)")

    # Entity aliases
    op.execute("""
        CREATE TABLE core.entity_aliases (
            entity_id uuid NOT NULL REFERENCES core.entities(id) ON DELETE CASCADE,
            alias text NOT NULL,
            alias_type text,
            PRIMARY KEY (entity_id, alias)
        )
    """)
    op.execute("CREATE INDEX entity_aliases_alias_trgm ON core.entity_aliases USING gin (alias gin_trgm_ops)")

    # Mentions
    op.create_table(
        "mentions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.Uuid, sa.ForeignKey("core.entities.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("span_start", sa.Integer),
        sa.Column("span_end", sa.Integer),
        sa.Column("surface_form", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="core",
    )
    op.create_index("mentions_passage_idx", "mentions", ["passage_id"], schema="core")
    op.create_index("mentions_entity_idx", "mentions", ["entity_id"], schema="core")

    # Events
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("timestamp_start", sa.DateTime(timezone=True)),
        sa.Column("timestamp_end", sa.DateTime(timezone=True)),
        sa.Column("precision", sa.Text),
        sa.Column("location_id", sa.Uuid, sa.ForeignKey("core.entities.id")),
        sa.Column("location_text", sa.Text),
        sa.Column("source_passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="SET NULL")),
        sa.Column("payload", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="core",
    )
    op.create_index("events_type_idx", "events", ["event_type"], schema="core")

    # Event actors
    op.execute("""
        CREATE TABLE core.event_actors (
            event_id uuid NOT NULL REFERENCES core.events(id) ON DELETE CASCADE,
            entity_id uuid NOT NULL REFERENCES core.entities(id) ON DELETE RESTRICT,
            role text NOT NULL,
            PRIMARY KEY (event_id, entity_id, role)
        )
    """)
    op.create_index("event_actors_entity_idx", "event_actors", ["entity_id"], schema="core")

    # Edges
    op.create_table(
        "edges",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_id", sa.Uuid, nullable=False),
        sa.Column("target_kind", sa.Text, nullable=False),
        sa.Column("target_id", sa.Uuid, nullable=False),
        sa.Column("relation_type", sa.Text, nullable=False),
        sa.Column("attributes", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("source_passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="SET NULL")),
        sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="core",
    )
    op.create_index("edges_source_idx", "edges", ["source_kind", "source_id"], schema="core")
    op.create_index("edges_target_idx", "edges", ["target_kind", "target_id"], schema="core")
    op.create_index("edges_relation_idx", "edges", ["relation_type"], schema="core")

    # LLM calls
    op.create_table(
        "llm_calls",
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
        schema="core",
    )

    # Extraction schemas
    op.create_table(
        "extraction_schemas",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("schema", sa.JSON, nullable=False),
        sa.Column("prompt_template", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("name", "version", "owner"),
        schema="core",
    )

    # Extractions
    op.create_table(
        "extractions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schema_id", sa.Uuid, sa.ForeignKey("core.extraction_schemas.id"), nullable=False),
        sa.Column("extractor_version", sa.Text, nullable=False),
        sa.Column("llm_model", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("records", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("llm_call_id", sa.Uuid),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("passage_id", "schema_id", "extractor_version"),
        schema="core",
    )

    # Extraction records
    op.create_table(
        "extraction_records",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("extraction_id", sa.Uuid, sa.ForeignKey("core.extractions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("passage_id", sa.Uuid, sa.ForeignKey("core.passages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("schema_id", sa.Uuid, sa.ForeignKey("core.extraction_schemas.id"), nullable=False),
        sa.Column("record_type", sa.Text, nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("evidence_start", sa.Integer),
        sa.Column("evidence_end", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="core",
    )
    op.create_index("extraction_records_passage_idx", "extraction_records", ["passage_id"], schema="core")
    op.create_index("extraction_records_type_idx", "extraction_records", ["record_type"], schema="core")

    # Ingestion runs
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("source_spec", sa.JSON, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("stats", sa.JSON, nullable=False, server_default="{}"),
        schema="core",
    )

    # Ingestion items
    op.create_table(
        "ingestion_items",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("core.ingestion_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column("document_id", sa.Uuid, sa.ForeignKey("core.documents.id")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="core",
    )
    op.create_index("ingestion_items_run_idx", "ingestion_items", ["run_id"], schema="core")

    # Installed packs
    op.create_table(
        "installed_packs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("manifest", sa.JSON, nullable=False),
        sa.Column("permissions_granted", sa.JSON, nullable=False, server_default="{}"),
        schema="core",
    )


def downgrade() -> None:
    for table in [
        "installed_packs", "ingestion_items", "ingestion_runs",
        "extraction_records", "extractions", "extraction_schemas",
        "llm_calls", "edges", "event_actors", "events",
        "mentions", "entity_aliases", "entities",
        "passage_fts", "passage_embeddings", "passages", "documents",
    ]:
        op.execute(f"DROP TABLE IF EXISTS core.{table} CASCADE")
    op.execute("DROP SCHEMA IF EXISTS core CASCADE")
