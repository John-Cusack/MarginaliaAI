"""Add a natural-key unique index on edges for dedup.

Lets ``PGEdgeRepo.upsert`` insert-or-update on the
(source_kind, source_id, target_kind, target_id, relation_type) triple so
re-running extractions (e.g. citation extraction) enriches existing edges
instead of duplicating them.

Revision ID: 002_edge_dedup
Create Date: 2026-06-29
"""

from alembic import op

revision = "002_edge_dedup"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Collapse any pre-existing duplicates before adding the unique index,
    # keeping the most recently created row per natural key.
    op.execute(
        """
        DELETE FROM core.edges e
        USING core.edges dup
        WHERE e.source_kind = dup.source_kind
          AND e.source_id = dup.source_id
          AND e.target_kind = dup.target_kind
          AND e.target_id = dup.target_id
          AND e.relation_type = dup.relation_type
          AND e.created_at < dup.created_at
        """
    )
    op.create_index(
        "edges_natural_key_uq",
        "edges",
        ["source_kind", "source_id", "target_kind", "target_id", "relation_type"],
        unique=True,
        schema="core",
    )


def downgrade() -> None:
    op.drop_index("edges_natural_key_uq", table_name="edges", schema="core")
