"""Promote passage offsets from JSON locator to indexed columns.

The `locator` JSON stays for type-specific extras (page, verse, timecode), but a
span of the document is an address, not a decoration: it needs to be indexed and
range-queried.

Nullable at first so existing rows survive. A follow-up migration sets NOT NULL
once `reindex chunks` has re-anchored the corpus — until then, passages written
by the 1.0 chunkers have no trustworthy offsets to backfill from
(`prose_window` 1.0 wrote `byte_start: 0` for every passage).

Revision ID: 004_passage_offsets
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "004_passage_offsets"
down_revision = "003_document_texts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("passages", sa.Column("char_start", sa.Integer), schema="core")
    op.add_column("passages", sa.Column("char_end", sa.Integer), schema="core")
    op.execute(
        "CREATE INDEX passages_doc_span_idx "
        "ON core.passages (document_id, char_start, char_end)"
    )


def downgrade() -> None:
    op.drop_index("passages_doc_span_idx", table_name="passages", schema="core")
    op.drop_column("passages", "char_end", schema="core")
    op.drop_column("passages", "char_start", schema="core")
