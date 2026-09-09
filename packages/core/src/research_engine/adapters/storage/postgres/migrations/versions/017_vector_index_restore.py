"""Rebuild the HNSW index migration 006 created and something later dropped.

Migration 006 typed `passage_embeddings.embedding` as `vector(1024)` and built
an HNSW index on it. On this database the column is still typed and the index is
gone — so 006 ran, and the index was dropped afterwards by something that left
no record. Alembic will not re-run 006, so the repair has to be its own
revision.

The loss was invisible, which is the more important half of this change.
`schema.py` declared the column as `Vector()` with no dimension and no
`sa.Index`, so nothing described the index that was supposed to exist; and
`test_schema_truthfulness` asserted only `declared - actual`, which cannot see
an index the database has lost because the declaration never had it either.
Both halves are fixed alongside this: the index is declared in `schema.py`, and
the test now also asserts `actual - declared` over the tables it knows.

Measured on this corpus (94,858 vectors) before the rebuild: a nearest-neighbour
query was a parallel sequential scan at ~275 ms warm.

`IF NOT EXISTS` because the whole point is that this revision may run against a
database where 006's index survived.

Revision ID: 017_vector_index_restore
Create Date: 2026-09-08
"""

import sqlalchemy as sa
from alembic import op

revision = "017_vector_index_restore"
down_revision = "016_versification"
branch_labels = None
depends_on = None

DIM = 1024
INDEX_NAME = "passage_embeddings_hnsw"


def upgrade() -> None:
    conn = op.get_bind()

    # 006 typed the column before building the index, and an HNSW index cannot
    # be built on a `vector` with no dimension. If 006's ALTER was rolled back
    # too, say so rather than failing inside the build.
    typed = conn.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = 'core.passage_embeddings'::regclass "
            "  AND a.attname = 'embedding'"
        )
    ).scalar()
    if typed != f"vector({DIM})":
        raise RuntimeError(
            f"core.passage_embeddings.embedding is {typed!r}, not vector({DIM}). "
            f"An HNSW index needs a dimensioned column; re-run migration 006's "
            f"ALTER before this revision."
        )

    # The build is memory-hungry and Docker's default 64 MB of shared memory is
    # not enough — tools/dev-postgres/docker-compose.yml sets shm_size: 4gb.
    conn.execute(sa.text("SET maintenance_work_mem = '2GB'"))
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON core.passage_embeddings "
        f"USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS core.{INDEX_NAME}")
