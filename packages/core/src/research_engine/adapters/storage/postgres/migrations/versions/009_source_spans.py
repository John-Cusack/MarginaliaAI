"""The span table: one row per cited address.

A quotation is a span of a *document*, not of a passage — passages are deleted
and reinserted on every re-chunk, so anchoring to `passage_id` means losing the
anchor on every re-chunk. This table owns the address `(document_id,
char_start, char_end)` and the canonical slice at it; every writer of a span
goes through the resolver, which is why the coordinates are unique and no
writer inserts here directly.

`passage_id` is a `SET NULL` cache for the one overlapping passage, there so a
reader can jump without a range scan. It carries no meaning: re-chunking may
null it, and the overlap query rebuilds it at read time.

Revision ID: 009_source_spans
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "009_source_spans"
down_revision = "008_passage_node"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA evidence")
    op.create_table(
        "source_spans",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("core.documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        # The canonical slice document_texts.text[char_start:char_end], written
        # by the resolver. NOT the author's typed quote: that lives on the
        # citing row, which is what lets two citers disagree about one span.
        sa.Column("quoted_text", sa.Text, nullable=False),
        sa.Column("parser", sa.Text),
        sa.Column("parser_version", sa.Text),
        sa.Column(
            "passage_id",
            sa.Uuid,
            sa.ForeignKey("core.passages.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end > char_start", name="source_spans_range_ck"
        ),
        # Span identity: one row per (document, coordinates). The unique index
        # also serves range lookups; no separate index.
        sa.UniqueConstraint(
            "document_id", "char_start", "char_end",
            name="source_spans_coordinates_uk",
        ),
        schema="evidence",
    )


def downgrade() -> None:
    op.drop_table("source_spans", schema="evidence")
    op.execute("DROP SCHEMA evidence")
