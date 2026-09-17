"""Replace copied-pack installation rows with distribution activation audit rows.

Revision ID: 019_plugin_activations
Revises: 018_anchor_editions
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision = "019_plugin_activations"
down_revision = "018_anchor_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("installed_packs", "plugin_activations", schema="core")
    op.execute(
        "ALTER TABLE core.plugin_activations "
        "RENAME CONSTRAINT installed_packs_pkey TO plugin_activations_pkey"
    )
    op.alter_column(
        "plugin_activations",
        "id",
        new_column_name="plugin_id",
        schema="core",
    )
    op.alter_column(
        "plugin_activations",
        "version",
        new_column_name="distribution_version",
        schema="core",
    )
    op.alter_column(
        "plugin_activations",
        "source_url",
        new_column_name="legacy_source_url",
        nullable=True,
        existing_type=sa.Text(),
        schema="core",
    )
    op.alter_column(
        "plugin_activations",
        "source_ref",
        new_column_name="legacy_source_ref",
        nullable=True,
        existing_type=sa.Text(),
        schema="core",
    )

    op.add_column(
        "plugin_activations",
        sa.Column("distribution_name", sa.Text(), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("entry_point_name", sa.Text(), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("manifest_sha256", sa.Text(), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("last_error", sa.Text(), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("provenance", sa.JSON(), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("database_revision", sa.Integer(), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column("database_status", sa.Text(), nullable=True),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
        schema="core",
    )
    op.add_column(
        "plugin_activations",
        sa.Column(
            "approved_non_interactive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="core",
    )
    op.execute(
        "UPDATE core.plugin_activations "
        "SET enabled = false, state = 'legacy'"
    )


def downgrade() -> None:
    # Rows approved after 0.6 have no Git provenance. Preserve them by deriving
    # stable audit strings before restoring the legacy NOT NULL shape.
    op.execute(
        "UPDATE core.plugin_activations SET "
        "legacy_source_url = COALESCE("
        "legacy_source_url, provenance ->> 'url', "
        "'distribution:' || COALESCE(distribution_name, plugin_id)), "
        "legacy_source_ref = COALESCE("
        "legacy_source_ref, distribution_version, manifest_sha256, 'unknown')"
    )

    for column in [
        "database_status",
        "database_revision",
        "approved_non_interactive",
        "state",
        "provenance",
        "last_error",
        "last_seen_at",
        "approved_at",
        "manifest_sha256",
        "entry_point_name",
        "distribution_name",
    ]:
        op.drop_column("plugin_activations", column, schema="core")

    op.alter_column(
        "plugin_activations",
        "legacy_source_ref",
        new_column_name="source_ref",
        nullable=False,
        existing_type=sa.Text(),
        schema="core",
    )
    op.alter_column(
        "plugin_activations",
        "legacy_source_url",
        new_column_name="source_url",
        nullable=False,
        existing_type=sa.Text(),
        schema="core",
    )
    op.alter_column(
        "plugin_activations",
        "distribution_version",
        new_column_name="version",
        schema="core",
    )
    op.alter_column(
        "plugin_activations",
        "plugin_id",
        new_column_name="id",
        schema="core",
    )
    op.execute(
        "ALTER TABLE core.plugin_activations "
        "RENAME CONSTRAINT plugin_activations_pkey TO installed_packs_pkey"
    )
    op.rename_table("plugin_activations", "installed_packs", schema="core")
