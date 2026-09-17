"""Rename the edition identifier: `zotero_key` becomes `edition_key`.

The key never touched Zotero's servers — it is a plain string naming an
edition, stored in this database — but the name told every reader otherwise.
This renames the three columns that hold it (`bibliography.editions`,
`authored.citation_items`, `argument.anchors`), the index and unique
constraint named for it, and the `core.documents.metadata` JSON key that
feeds the editions table, preserving every value. No frozen work exists
yet, so no stored content hash or waiver row names the old field.

Revision ID: 013_edition_key
Create Date: 2026-09-06
"""

from alembic import op

revision = "013_edition_key"
down_revision = "012_authored_and_bibliography"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "editions", "zotero_key", new_column_name="edition_key", schema="bibliography"
    )
    op.execute(
        "ALTER TABLE bibliography.editions "
        "RENAME CONSTRAINT editions_zotero_key_key TO editions_edition_key_key"
    )
    op.alter_column(
        "citation_items", "zotero_key", new_column_name="edition_key", schema="authored"
    )
    op.execute(
        "ALTER INDEX authored.citation_items_zotero_idx "
        "RENAME TO citation_items_edition_key_idx"
    )
    op.alter_column(
        "anchors", "zotero_key", new_column_name="edition_key", schema="argument"
    )
    # The ingest hook reads this JSON key into bibliography.editions; move
    # the stored keys with the column so keyed documents stay joined.
    # metadata is a json column: cast to jsonb for the key surgery, with an
    # implicit assignment cast back on write.
    op.execute(
        "UPDATE core.documents SET metadata = "
        "(metadata::jsonb - 'zotero_key') || "
        "jsonb_build_object('edition_key', metadata->'zotero_key') "
        "WHERE metadata->'zotero_key' IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE core.documents SET metadata = "
        "(metadata::jsonb - 'edition_key') || "
        "jsonb_build_object('zotero_key', metadata->'edition_key') "
        "WHERE metadata->'edition_key' IS NOT NULL"
    )
    op.alter_column(
        "anchors", "edition_key", new_column_name="zotero_key", schema="argument"
    )
    op.execute(
        "ALTER INDEX authored.citation_items_edition_key_idx "
        "RENAME TO citation_items_zotero_idx"
    )
    op.alter_column(
        "citation_items", "edition_key", new_column_name="zotero_key", schema="authored"
    )
    op.execute(
        "ALTER TABLE bibliography.editions "
        "RENAME CONSTRAINT editions_edition_key_key TO editions_zotero_key_key"
    )
    op.alter_column(
        "editions", "edition_key", new_column_name="zotero_key", schema="bibliography"
    )
