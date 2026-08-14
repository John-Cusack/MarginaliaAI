"""Canonical document text — the substrate passage offsets address.

Separate from ``core.documents`` because that table is on the search hydration
hot path and should not drag a megabyte per row.

``parser`` / ``parser_version`` are recorded because docling output is not
guaranteed stable across versions: if it drifts, this stored text — not the
source file — is the addressing substrate, and re-parsing a document becomes a
re-anchoring event.

``normalized_text`` and its trigram index exist for quote verification; the raw
``text`` is what offsets address. ``normalization_version`` lets normalization
evolve without invalidating stored spans.

Revision ID: 003_document_texts
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "003_document_texts"
down_revision = "002_edge_dedup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_texts",
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("normalized_text", sa.Text, nullable=False),
        sa.Column("normalization_version", sa.Text, nullable=False),
        sa.Column("parser", sa.Text, nullable=False),
        sa.Column("parser_version", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        schema="core",
    )
    op.execute(
        "CREATE INDEX document_texts_norm_trgm "
        "ON core.document_texts USING gin (normalized_text gin_trgm_ops)"
    )


def downgrade() -> None:
    op.drop_index("document_texts_norm_trgm", table_name="document_texts", schema="core")
    op.drop_table("document_texts", schema="core")
