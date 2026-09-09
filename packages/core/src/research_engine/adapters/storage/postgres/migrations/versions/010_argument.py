"""The claim ledger: claims, their edges, and the anchors that ground them.

Claims are the unit the reasoner works on; edges are how they stand on each
other; anchors are where they touch the corpus. The anchor keeps its own typed
quote and tier and references the span for its address — decision 1 — so two
anchors over one span keep their own wording and their own verification, and
re-verifying one never rewrites the other.

`verify_status` keeps only the three storable tiers. `not_found` and
`no_canonical_text` are answers, not rows: nothing is stored for them, which
the check constraint enforces before any application code runs.

`argument.derivations` is deliberately absent (default: omit unless master
§11 item 5 is decided yes). `verify_attempts` is a separate later migration,
not part of 009 or this one: decision 4 says first need, and this step is
not it.

Revision ID: 010_argument
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "010_argument"
down_revision = "009_source_spans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA argument")
    op.create_table(
        "claims",
        sa.Column("id", sa.Uuid, primary_key=True),
        # 'JUB-004', the handle you cite. Unique across the dossier, the
        # essay, the script, and everything else.
        sa.Column("ref", sa.Text, nullable=False, unique=True),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("confidence", sa.Float),
        sa.Column("steelman", sa.Text),
        sa.Column("public_ready", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "academic_candidate", sa.Boolean, nullable=False, server_default="false"
        ),
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
            "status IN "
            "('open','researching','rebutted','weakened','unresolved','conceded')",
            name="claims_status_ck",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1",
            name="claims_conf_ck",
        ),
        schema="argument",
    )
    op.create_table(
        "claim_edges",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "source_id",
            sa.Uuid,
            sa.ForeignKey("argument.claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_id",
            sa.Uuid,
            sa.ForeignKey("argument.claims.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relation", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("note", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_id", "target_id", "relation"),
        sa.CheckConstraint("source_id <> target_id", name="claim_edges_no_self"),
        schema="argument",
    )
    op.create_index(
        "claim_edges_target_idx", "claim_edges", ["target_id", "relation"],
        schema="argument",
    )
    op.create_index(
        "claim_edges_source_idx", "claim_edges", ["source_id", "relation"],
        schema="argument",
    )
    op.create_table(
        "anchors",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "claim_id",
            sa.Uuid,
            sa.ForeignKey("argument.claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column(
            "person_entity_id",
            sa.Uuid,
            sa.ForeignKey("core.entities.id", ondelete="SET NULL"),
        ),
        # Decision 1: the address is the shared span; the quote and tier are
        # this row's.
        sa.Column(
            "source_span_id",
            sa.Uuid,
            sa.ForeignKey("evidence.source_spans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quoted_text", sa.Text, nullable=False),
        sa.Column("verify_status", sa.Text),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("parser_version", sa.Text),
        sa.Column("edition", sa.Text),
        sa.Column("zotero_key", sa.Text),
        sa.Column("locator", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('asserts','supports','rebuts','context')",
            name="anchors_role_ck",
        ),
        sa.CheckConstraint(
            "verify_status IS NULL OR verify_status IN ('exact','normalized','near')",
            name="anchors_verify_ck",
        ),
        schema="argument",
    )
    op.create_index(
        "anchors_claim_idx", "anchors", ["claim_id"], schema="argument"
    )
    op.create_index(
        "anchors_span_idx", "anchors", ["source_span_id"], schema="argument"
    )


def downgrade() -> None:
    op.drop_index("anchors_span_idx", table_name="anchors", schema="argument")
    op.drop_index("anchors_claim_idx", table_name="anchors", schema="argument")
    op.drop_table("anchors", schema="argument")
    op.drop_index(
        "claim_edges_source_idx", table_name="claim_edges", schema="argument"
    )
    op.drop_index(
        "claim_edges_target_idx", table_name="claim_edges", schema="argument"
    )
    op.drop_table("claim_edges", schema="argument")
    op.drop_table("claims", schema="argument")
    op.execute("DROP SCHEMA argument")
