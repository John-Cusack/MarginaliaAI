"""Machine claims: extraction records turned into anchored, queryable rows.

An extraction record is an answer a model gave about one passage. These tables
turn the claim-shaped ones into something the later funnel stages can compare
without re-reading JSON:

- **`dilemma.claims`**: one row per extracted claim (and one per vouch, which
  is also something a passage asserts). The label columns keep exactly what
  the extraction wrote; the entity columns are set only when a label resolved
  exactly, and the ``*_ref`` columns carry a seeded or hint id verbatim for
  the profile layer to resolve later. ``(extraction_record_id, statement)``
  is unique, which is what makes materializing the same records twice a
  no-op rather than a doubling.
- **`dilemma.claim_evidence`**: every verbatim evidence field of the record,
  resolved through the span resolver to `evidence.source_spans`, in schema
  declaration order. A claim that cannot be traced to its sentences is not
  stored at all — that check is the materializer's, these rows are its proof.
- **`dilemma.vouch_edges`**: what one voice or text is said to do for
  another's standing, hanging off the claim row that asserts it.

The vocabularies are the engine's own, as with source roles in 022; what a
topic key or a ref means lives in profile data, outside the schema.

Additive: no existing table changes, and the `dilemma` schema (022's) stays.

Revision ID: 023_machine_claims
Revises: 022_dilemma_profiles
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "023_machine_claims"
down_revision = "022_dilemma_profiles"
branch_labels = None
depends_on = None

POLARITIES = ("affirms", "denies")
MODALITIES = (
    "asserts",
    "commands",
    "prophesies",
    "reports_other_view",
    "quotes_scripture",
    "hypothetical",
    "question",
)
VOUCH_KINDS = (
    "affirms_reliability",
    "affirms_authority",
    "claims_prophesied_by",
    "self_reliability",
    "affirms_fallibility",
)
TARGET_FORMS = ("work_as_given", "extant_edition", "unspecified")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "extraction_record_id",
            sa.Uuid,
            sa.ForeignKey("core.extraction_records.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("core.documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "edition_id",
            sa.Uuid,
            sa.ForeignKey("bibliography.editions.id", ondelete="RESTRICT"),
        ),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("subject_label", sa.Text),
        sa.Column("subject_ref", sa.Text),
        sa.Column(
            "subject_entity_id",
            sa.Uuid,
            sa.ForeignKey("core.entities.id", ondelete="SET NULL"),
        ),
        sa.Column("predicate", sa.Text),
        sa.Column("object_label", sa.Text),
        sa.Column("polarity", sa.Text, nullable=False, server_default="affirms"),
        sa.Column("modality", sa.Text, nullable=False),
        sa.Column("speaker_label", sa.Text),
        sa.Column("speaker_ref", sa.Text),
        sa.Column(
            "speaker_entity_id",
            sa.Uuid,
            sa.ForeignKey("core.entities.id", ondelete="SET NULL"),
        ),
        sa.Column("scope_quantifier", sa.Text),
        sa.Column("temporal_scope", sa.Text),
        sa.Column(
            "topic_keys", ARRAY(sa.Text), nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("confidence", sa.Float),
        sa.Column("attributes", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"polarity IN {_in(POLARITIES)}", name="claims_polarity_ck"
        ),
        sa.CheckConstraint(
            f"modality IN {_in(MODALITIES)}", name="claims_modality_ck"
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="claims_confidence_ck"
        ),
        # Materialization idempotency: re-running over the same records must
        # not double the claims. NULL record ids never collide, as usual.
        sa.UniqueConstraint(
            "extraction_record_id", "statement", name="claims_record_statement_uq"
        ),
        schema="dilemma",
    )
    op.create_index("claims_run_idx", "claims", ["run_id"], schema="dilemma")
    op.create_index(
        "claims_document_idx", "claims", ["document_id"], schema="dilemma"
    )

    op.create_table(
        "claim_evidence",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "claim_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_span_id",
            sa.Uuid,
            sa.ForeignKey("evidence.source_spans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("field", sa.Text, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.UniqueConstraint("claim_id", "ordinal", name="claim_evidence_ordinal_uq"),
        schema="dilemma",
    )

    op.create_table(
        "vouch_edges",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "claim_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("target_label", sa.Text),
        sa.Column("target_ref", sa.Text),
        sa.Column(
            "target_entity_id",
            sa.Uuid,
            sa.ForeignKey("core.entities.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "target_edition_id",
            sa.Uuid,
            sa.ForeignKey("bibliography.editions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "target_form", sa.Text, nullable=False, server_default="unspecified"
        ),
        sa.Column("attributes", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(f"kind IN {_in(VOUCH_KINDS)}", name="vouch_edges_kind_ck"),
        sa.CheckConstraint(
            f"target_form IN {_in(TARGET_FORMS)}", name="vouch_edges_target_form_ck"
        ),
        schema="dilemma",
    )
    op.create_index("vouch_edges_run_idx", "vouch_edges", ["run_id"], schema="dilemma")


def downgrade() -> None:
    # Reverse creation order. The `dilemma` schema is 022's and stays.
    op.drop_table("vouch_edges", schema="dilemma")
    op.drop_table("claim_evidence", schema="dilemma")
    op.drop_table("claims", schema="dilemma")
