"""Type the embedding column and give it an HNSW index.

Before this, `passage_embeddings` had only a btree primary key, so every
semantic search was a sequential scan over the whole table — measured at
1,490 ms on a 271k-vector corpus.

Two parts, and the order matters:

1. **Type the column.** `vector` without a dimension cannot carry a usable HNSW
   index; you can index a cast expression instead, but then every query has to
   reproduce that expression exactly or the planner silently ignores the index.
   Typing the column makes the index ordinary and the matching trivial.
2. **Build the index.** Not CONCURRENTLY: the ALTER above already takes an
   ACCESS EXCLUSIVE lock and rewrites the table, so a concurrent build buys
   nothing and would force this out of a transaction, making a partial failure
   possible.

The ALTER rewrites ~1.5 GB and the build needs a large `maintenance_work_mem`
and more than Docker's default 64 MB of shared memory — see
`tools/dev-postgres/docker-compose.yml`.

Revision ID: 006_vector_index
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "006_vector_index"
down_revision = "005_missing_indexes"
branch_labels = None
depends_on = None

DIM = 1024
INDEX_NAME = "passage_embeddings_hnsw"


def upgrade() -> None:
    conn = op.get_bind()

    # Typing the column silently truncates nothing and loudly fails on a
    # mismatch — but the failure deep inside a rewrite is hard to read, so check
    # first and say what to do about it.
    offending = conn.execute(
        sa.text(
            "SELECT dim, count(*) FROM core.passage_embeddings "
            "WHERE dim <> :dim GROUP BY dim"
        ),
        {"dim": DIM},
    ).all()
    if offending:
        detail = ", ".join(f"dim {row[0]}: {row[1]} rows" for row in offending)
        raise RuntimeError(
            f"Cannot type embedding as vector({DIM}) — {detail}. "
            f"Remove them first: `research-engine embeddings purge <model>` for "
            f"vectors from a superseded or test model, then "
            f"`research-engine embeddings backfill` to fill the gaps."
        )

    conn.execute(sa.text("SET maintenance_work_mem = '2GB'"))
    op.execute(
        f"ALTER TABLE core.passage_embeddings "
        f"ALTER COLUMN embedding TYPE vector({DIM})"
    )
    op.execute(
        f"CREATE INDEX {INDEX_NAME} ON core.passage_embeddings "
        f"USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS core.{INDEX_NAME}")
    op.execute("ALTER TABLE core.passage_embeddings ALTER COLUMN embedding TYPE vector")
