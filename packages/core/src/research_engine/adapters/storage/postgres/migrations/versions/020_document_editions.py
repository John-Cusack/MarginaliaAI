"""Link documents to their stable bibliography edition.

Revision ID: 020_document_editions
Revises: 019_plugin_activations
Create Date: 2026-09-18
"""

import sqlalchemy as sa
from alembic import op

revision = "020_document_editions"
down_revision = "019_plugin_activations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("edition_id", sa.Uuid(), nullable=True),
        schema="core",
    )

    # Seven live keys existed only in document metadata. Manufacturing edition
    # rows from anything else would be guesswork; these keys are already the
    # corpus's declared identity and are safe to materialize before the FK.
    op.execute(
        "INSERT INTO bibliography.editions (id, edition_key) "
        "SELECT gen_random_uuid(), keys.edition_key "
        "FROM ("
        "  SELECT DISTINCT btrim(metadata->>'edition_key') AS edition_key "
        "  FROM core.documents "
        "  WHERE NULLIF(btrim(metadata->>'edition_key'), '') IS NOT NULL"
        ") AS keys "
        "ON CONFLICT (edition_key) DO NOTHING"
    )
    op.execute(
        "UPDATE core.documents AS d SET edition_id = e.id "
        "FROM bibliography.editions AS e "
        "WHERE e.edition_key = btrim(d.metadata->>'edition_key')"
    )

    op.create_foreign_key(
        "documents_edition_id_fk",
        "documents",
        "editions",
        ["edition_id"],
        ["id"],
        source_schema="core",
        referent_schema="bibliography",
        ondelete="RESTRICT",
    )
    op.create_index(
        "documents_edition_idx",
        "documents",
        ["edition_id"],
        schema="core",
    )


def downgrade() -> None:
    op.drop_index("documents_edition_idx", table_name="documents", schema="core")
    op.drop_constraint(
        "documents_edition_id_fk",
        "documents",
        schema="core",
        type_="foreignkey",
    )
    op.drop_column("documents", "edition_id", schema="core")
