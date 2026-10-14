"""Contradiction-finder funnel: claim vectors and scored pairs.

Stage 1 of the funnel compares claims by meaning, not by words, so each
eligible claim's statement gets one embedding row; Stage 2 asks a model
whether the closest pairs contradict, and keeps the verdicts here:

- **`dilemma.claim_embeddings`**: one vector per
  ``(claim_id, model, model_version)``, over the claim's `statement` text
  only. Keyed and indexed exactly like `core.passage_embeddings` — same
  bge-m3 width, same HNSW cosine shape — so the same search pattern works
  on both tables.
- **`dilemma.pair_scores`**: one NLI verdict per ``(run_id, claim_a_id,
  claim_b_id)``, with the pair ordered (``claim_a_id < claim_b_id`` by
  string compare) so the same pair never lands twice under two orders.
  Inserting is ``ON CONFLICT DO NOTHING`` for the same reason. The
  ``(run, a, b, prompt_hash, model)`` columns record exactly what produced
  the verdict, so Stage 2 can skip pairs it already scored.

Ordering is a service convention, not a constraint: Postgres cannot compare
uuids with ``<`` in a check constraint the way the service's string compare
does, and a second expression index would defend nothing the primary key
does not already key.

Additive: no existing table changes.

Revision ID: 024_funnel_pairs
Revises: 023_machine_claims
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "024_funnel_pairs"
down_revision = "023_machine_claims"
branch_labels = None
depends_on = None

DIM = 1024
RELATIONS = ("entail", "contradict", "neutral")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "claim_embeddings",
        sa.Column(
            "claim_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("model_version", sa.Text, nullable=False),
        sa.Column("embedding", sa.Text, nullable=False),
        schema="dilemma",
    )
    # The vector type has no Alembic/SQLAlchemy DDL binding in this project —
    # 001 declares it in raw SQL for the same reason — so the dimensioned
    # column is typed after creation, exactly as 006 does for passages.
    op.execute(
        f"ALTER TABLE dilemma.claim_embeddings "
        f"ALTER COLUMN embedding TYPE vector({DIM}) USING embedding::vector"
    )
    op.execute(
        "ALTER TABLE dilemma.claim_embeddings "
        "ADD PRIMARY KEY (claim_id, model, model_version)"
    )
    op.execute(
        "CREATE INDEX claim_embeddings_hnsw ON dilemma.claim_embeddings "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "pair_scores",
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "claim_a_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "claim_b_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column(
            "llm_call_id",
            sa.Uuid,
            sa.ForeignKey("core.llm_calls.id", ondelete="SET NULL"),
        ),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_hash", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"relation IN {_in(RELATIONS)}", name="pair_scores_relation_ck"
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="pair_scores_confidence_ck"
        ),
        # Never a pair with itself; ordering (a < b) is the writer's convention.
        sa.CheckConstraint("claim_a_id <> claim_b_id", name="pair_scores_no_self_ck"),
        sa.PrimaryKeyConstraint("run_id", "claim_a_id", "claim_b_id"),
        schema="dilemma",
    )
    op.create_index(
        "pair_scores_run_idx", "pair_scores", ["run_id"], schema="dilemma"
    )


def downgrade() -> None:
    op.drop_table("pair_scores", schema="dilemma")
    op.drop_table("claim_embeddings", schema="dilemma")
