"""Create the index `schema.py` declared but migration 001 never built.

Reconciling the SQLAlchemy metadata against the live database turned up four
declared indexes that do not exist. Three were GIN indexes on `json` columns,
which Postgres rejects outright — those declarations were removed as fiction.
This one is real and useful: `extractions.schema_id` is the join column for
`query_records`, and without it every extraction query scans the table.

Revision ID: 005_missing_indexes
Create Date: 2026-08-10
"""

from alembic import op

revision = "005_missing_indexes"
down_revision = "004_passage_offsets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "extractions_schema_idx", "extractions", ["schema_id"], schema="core"
    )


def downgrade() -> None:
    op.drop_index("extractions_schema_idx", table_name="extractions", schema="core")
