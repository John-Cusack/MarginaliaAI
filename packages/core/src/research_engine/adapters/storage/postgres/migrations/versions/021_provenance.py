"""Source provenance and text marks.

Three facts about a source had nowhere to live, and each is needed before a
corpus can be built deliberately rather than accumulated:

- **Where an edition came from, and on what terms.** An acquisition records the
  method, URL, licence and retrieval time for one edition, and its raw
  artifacts by SHA-256. An edition that cannot be had — out of print, behind a
  paywall, in copyright and print-only — is a row too, `blocked`, with the
  reason and the way to get it. That is what turns "we don't have X" into a
  queue of scans and purchases rather than a silent gap. Every load is also
  counted against the source's known shape, in `coverage_reports`.
- **What an edition is an edition of.** Seven printings of one book are seven
  editions and one work; nothing said so. `editions.work_key` does, so a claim
  read once from one edition can be carried to the others, and two editions of
  one work are never mistaken for two witnesses that agree.
- **Ranges that do not nest.** `document_nodes` is one tree. A page break cuts
  across chapters, and a translator's supplied words cut across verses, so
  neither fits it. `document_marks` holds them as plain ranges over the
  canonical text, addressed like `core.words`.

Who holds an edition authoritative is not here: it is a fact about a reader,
not about the edition, and lives with the reader's profile (022).

Additive: one nullable column on `bibliography.editions`, otherwise new tables,
so nothing is re-chunked or re-embedded.

Revision ID: 021_provenance
Revises: 020_document_editions
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "021_provenance"
down_revision = "020_document_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("editions", sa.Column("work_key", sa.Text), schema="bibliography")
    op.create_index(
        "editions_work_key_idx", "editions", ["work_key"], schema="bibliography"
    )

    op.create_table(
        "acquisitions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "edition_id",
            sa.Uuid,
            sa.ForeignKey("bibliography.editions.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("collection", sa.Text, nullable=False, server_default="main"),
        sa.Column("status", sa.Text, nullable=False, server_default="planned"),
        sa.Column("method", sa.Text),
        sa.Column("url", sa.Text),
        sa.Column("license", sa.Text),
        sa.Column("license_notes", sa.Text),
        sa.Column("retrieved_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_reason", sa.Text),
        sa.Column("suggested_acquisition", sa.Text),
        sa.Column("manifest_ref", sa.Text),
        sa.Column("attributes", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('planned','acquired','loaded','blocked','failed')",
            name="acquisitions_status_ck",
        ),
        sa.CheckConstraint(
            "collection IN ('main','heldout_eval')",
            name="acquisitions_collection_ck",
        ),
        # A blocked source must say why, or the queue cannot be worked.
        sa.CheckConstraint(
            "status <> 'blocked' OR blocked_reason IS NOT NULL",
            name="acquisitions_blocked_has_reason",
        ),
        schema="bibliography",
    )
    op.create_index(
        "acquisitions_status_idx", "acquisitions", ["status"], schema="bibliography"
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "acquisition_id",
            sa.Uuid,
            sa.ForeignKey("bibliography.acquisitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.LargeBinary, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("bytes", sa.BigInteger, nullable=False),
        sa.Column("url", sa.Text),
        sa.Column("media_type", sa.Text),
        sa.Column(
            "retrieved_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("acquisition_id", "sha256"),
        sa.CheckConstraint("octet_length(sha256) = 32", name="artifacts_sha256_len"),
        sa.CheckConstraint("bytes >= 0", name="artifacts_bytes_ck"),
        schema="bibliography",
    )

    op.create_table(
        "coverage_reports",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "acquisition_id",
            sa.Uuid,
            sa.ForeignKey("bibliography.acquisitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("unit", sa.Text, nullable=False),
        sa.Column("expected_units", sa.Integer),
        sa.Column("loaded_units", sa.Integer, nullable=False),
        sa.Column("anomalies", JSONB, nullable=False, server_default="[]"),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("checker", sa.Text, nullable=False),
        sa.Column("checker_version", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        schema="bibliography",
    )
    op.create_index(
        "coverage_reports_acquisition_idx",
        "coverage_reports",
        ["acquisition_id", "created_at"],
        schema="bibliography",
    )

    op.create_table(
        "document_marks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("attrs", JSONB, nullable=False, server_default="{}"),
        # Zero width is allowed: a page break is a point, not a range.
        sa.CheckConstraint(
            "char_start >= 0 AND char_end >= char_start",
            name="document_marks_span_ck",
        ),
        schema="core",
    )
    op.create_index(
        "document_marks_doc_kind_idx",
        "document_marks",
        ["document_id", "kind", "char_start"],
        schema="core",
    )


def downgrade() -> None:
    op.drop_table("document_marks", schema="core")
    op.drop_table("coverage_reports", schema="bibliography")
    op.drop_table("artifacts", schema="bibliography")
    op.drop_table("acquisitions", schema="bibliography")
    op.drop_index("editions_work_key_idx", "editions", schema="bibliography")
    op.drop_column("editions", "work_key", schema="bibliography")
