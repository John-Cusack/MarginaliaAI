"""The Phase 1 spine: authored works and the editions stub.

Works live here from their first freeze: revisions own ordered blocks, blocks
own citation occurrences, occurrences own items over shared spans. Nothing
here stores prose twice — the block text lives once per revision, the span
slice once per address — and nothing here deletes what the ledger still
names: every consumer of a span REFERENCES it RESTRICT.

`bibliography.editions` is the decision-12 stub: one row per Zotero key seen
at ingest, backfilled here, maintained by the orchestrator. P3-1 extends it
and turns the document join into a FK; until then the join is by key.

Revision ID: 012_authored_and_bibliography
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "012_authored_and_bibliography"
down_revision = "010_argument"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS authored")
    op.execute("CREATE SCHEMA IF NOT EXISTS bibliography")

    op.create_table(
        "editions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("zotero_key", sa.Text, nullable=False, unique=True),
        sa.Column("csl", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        schema="bibliography",
    )
    # E.11: backfill in the same migration, in Python only if the project
    # prefers application-side UUIDs. gen_random_uuid() is core since PG13.
    op.execute(
        "INSERT INTO bibliography.editions (id, zotero_key) "
        "SELECT gen_random_uuid(), DISTINCT_KEYS.k "
        "FROM (SELECT DISTINCT metadata->>'zotero_key' AS k FROM core.documents "
        "WHERE metadata->>'zotero_key' IS NOT NULL) AS DISTINCT_KEYS "
        "ON CONFLICT (zotero_key) DO NOTHING"
    )

    op.create_table(
        "works",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("work_type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column("language", sa.Text),
        sa.Column("abstract", sa.Text),
        sa.Column("current_revision_id", sa.Uuid),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('draft','review','published','archived')",
            name="works_status_ck",
        ),
        schema="authored",
    )
    op.create_table(
        "work_revisions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "work_id",
            sa.Uuid,
            sa.ForeignKey("authored.works.id", ondelete="CASCADE"),
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
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("frozen_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.UniqueConstraint("id", "work_id"),
        sa.UniqueConstraint("work_id", "revision_number"),
        sa.CheckConstraint(
            "state IN ('draft','frozen','published','superseded')",
            name="work_revision_state_ck",
        ),
        schema="authored",
    )
    # Deferred: creating a work and its first revision in one transaction sets
    # both ends before commit, so the check waits until then.
    op.create_foreign_key(
        "works_current_revision_fk",
        "works",
        "work_revisions",
        ["current_revision_id", "id"],
        ["id", "work_id"],
        source_schema="authored",
        referent_schema="authored",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "work_blocks",
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
        sa.Column("attributes", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("id", "revision_id"),
        sa.UniqueConstraint("revision_id", "block_key"),
        # PG15: NULL parents are distinct positions, equal parents are not.
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
    op.create_index(
        "work_blocks_revision_idx", "work_blocks",
        ["revision_id", "parent_id", "position"], schema="authored",
    )
    op.create_table(
        "citation_occurrences",
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
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
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
    op.create_table(
        "citation_items",
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
        sa.Column("zotero_key", sa.Text),
        sa.Column(
            "source_span_id",
            sa.Uuid,
            sa.ForeignKey("evidence.source_spans.id", ondelete="RESTRICT"),
        ),
        # Decision 1: the typed quote, per citer — the span owns the slice.
        sa.Column("quoted_text", sa.Text),
        sa.Column("verify_status", sa.Text),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("locator", JSONB, nullable=False, server_default="{}"),
        sa.Column("prefix", sa.Text),
        sa.Column("suffix", sa.Text),
        sa.Column("suppress_author", sa.Boolean, nullable=False, server_default="false"),
        sa.PrimaryKeyConstraint("occurrence_id", "position"),
        sa.CheckConstraint(
            "edition_id IS NOT NULL OR zotero_key IS NOT NULL",
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
    op.create_index(
        "citation_items_span_idx", "citation_items", ["source_span_id"],
        schema="authored",
    )
    op.create_index(
        "citation_items_edition_idx", "citation_items", ["edition_id"],
        schema="authored",
    )
    op.create_index(
        "citation_items_zotero_idx", "citation_items", ["zotero_key"],
        schema="authored",
    )
    op.create_table(
        "block_source_links",
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
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("block_id", "source_span_id", "relation"),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name="block_source_conf_ck",
        ),
        schema="authored",
    )
    op.create_index(
        "block_source_links_span_idx", "block_source_links", ["source_span_id"],
        schema="authored",
    )
    # Decision 9: ships with 012 so the lemma query exists at the flip.
    op.create_table(
        "block_entity_links",
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
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("block_id", "entity_id", "relation"),
        schema="authored",
    )
    op.create_index(
        "block_entity_links_entity_idx", "block_entity_links",
        ["entity_id", "relation"], schema="authored",
    )
    # Name fixed by the guide: waivers are rows, never flags.
    op.create_table(
        "waivers",
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
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        schema="authored",
    )
    op.create_index(
        "waivers_revision_idx", "waivers", ["revision_id", "rule_id"],
        schema="authored",
    )


def downgrade() -> None:
    op.drop_index("waivers_revision_idx", table_name="waivers", schema="authored")
    op.drop_table("waivers", schema="authored")
    op.drop_index(
        "block_entity_links_entity_idx", table_name="block_entity_links",
        schema="authored",
    )
    op.drop_table("block_entity_links", schema="authored")
    op.drop_index(
        "block_source_links_span_idx", table_name="block_source_links",
        schema="authored",
    )
    op.drop_table("block_source_links", schema="authored")
    op.drop_index(
        "citation_items_zotero_idx", table_name="citation_items", schema="authored"
    )
    op.drop_index(
        "citation_items_edition_idx", table_name="citation_items", schema="authored"
    )
    op.drop_index(
        "citation_items_span_idx", table_name="citation_items", schema="authored"
    )
    op.drop_table("citation_items", schema="authored")
    op.drop_table("citation_occurrences", schema="authored")
    op.drop_index(
        "work_blocks_revision_idx", table_name="work_blocks", schema="authored"
    )
    op.drop_table("work_blocks", schema="authored")
    op.drop_constraint("works_current_revision_fk", "works", schema="authored")
    op.drop_table("work_revisions", schema="authored")
    op.drop_table("works", schema="authored")
    op.drop_table("editions", schema="bibliography")
    op.execute("DROP SCHEMA authored")
    op.execute("DROP SCHEMA bibliography")
