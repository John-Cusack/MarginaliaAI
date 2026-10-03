"""Give derived events a natural key: one per (event_type, source_passage_id).

Revision ID: 021_event_natural_key
Revises: 020_document_editions
Create Date: 2026-10-03

`core.events` had no key but its id, so a pass that derives events from
passages could only insert, and every re-run doubled the timeline. Edges solved
the same problem with `edges_natural_key_uq`; this is the events' equivalent.

Rows with no source passage are untouched: NULLs never conflict in a unique
index.
"""

import sqlalchemy as sa
from alembic import op

revision = "021_event_natural_key"
down_revision = "020_document_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT event_type, source_passage_id, count(*) AS n "
            "FROM core.events WHERE source_passage_id IS NOT NULL "
            "GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY n DESC LIMIT 5"
        )
    ).all()
    if duplicates:
        listed = "; ".join(
            f"{row.event_type} from passage {row.source_passage_id} x{row.n}"
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot add the events natural key: some passages already have more "
            f"than one event of the same type ({listed}). Remove the duplicates "
            "deliberately; choosing which to keep is not this migration's call."
        )
    op.create_index(
        "events_type_source_passage_uq",
        "events",
        ["event_type", "source_passage_id"],
        unique=True,
        schema="core",
    )


def downgrade() -> None:
    op.drop_index("events_type_source_passage_uq", table_name="events", schema="core")
