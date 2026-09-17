"""Give claim anchors a restricted bibliography edition identity.

External writers still name an edition by ``edition_key``. This revision joins
that stable key to ``bibliography.editions.id`` while retaining the key beside
the foreign key, matching authored citation items. A keyed anchor with no
bibliography row is a broken identity and aborts the migration instead of
manufacturing metadata.

Revision ID: 018_anchor_editions
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "018_anchor_editions"
down_revision = "017_vector_index_restore"
branch_labels = None
depends_on = None

FK_NAME = "anchors_edition_id_fk"
INDEX_NAME = "anchors_edition_idx"


def upgrade() -> None:
    op.add_column(
        "anchors",
        sa.Column("edition_id", sa.Uuid, nullable=True),
        schema="argument",
    )
    op.create_foreign_key(
        FK_NAME,
        "anchors",
        "editions",
        ["edition_id"],
        ["id"],
        source_schema="argument",
        referent_schema="bibliography",
        ondelete="RESTRICT",
    )
    op.create_index(
        INDEX_NAME,
        "anchors",
        ["edition_id"],
        schema="argument",
    )

    conn = op.get_bind()
    missing = conn.execute(
        sa.text(
            "SELECT array_agg(DISTINCT a.edition_key ORDER BY a.edition_key) "
            "FROM argument.anchors a "
            "LEFT JOIN bibliography.editions e "
            "  ON e.edition_key = a.edition_key "
            "WHERE a.edition_key IS NOT NULL AND e.id IS NULL"
        )
    ).scalar()
    if missing:
        raise RuntimeError(
            "Cannot backfill argument.anchors.edition_id: "
            f"unknown edition_key values {list(missing)!r}. "
            "Insert authoritative bibliography.editions rows before migrating."
        )

    op.execute(
        "UPDATE argument.anchors a SET edition_id = e.id "
        "FROM bibliography.editions e "
        "WHERE a.edition_key = e.edition_key"
    )
    op.drop_column("anchors", "edition", schema="argument")


def downgrade() -> None:
    op.add_column(
        "anchors",
        sa.Column("edition", sa.Text, nullable=True),
        schema="argument",
    )
    op.drop_index(INDEX_NAME, table_name="anchors", schema="argument")
    op.drop_constraint(FK_NAME, "anchors", schema="argument", type_="foreignkey")
    op.drop_column("anchors", "edition_id", schema="argument")
