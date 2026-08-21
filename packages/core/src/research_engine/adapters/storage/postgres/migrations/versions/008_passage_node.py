"""Link each passage to the node it sits in.

Both layers already address the same canonical text by span, so the containing
node is derivable at any time — but derivable is not the same as cheap. Climbing
from a search hit to the section it belongs to is the hot path of structural
retrieval: it is what turns twenty scattered hits into "three discussions", and
what turns `passage 214 of 440` into a citation someone can check. Doing that
with a range scan per hit puts a query between every result and its context.

Nullable, and ON DELETE SET NULL rather than CASCADE. Passages ingested before
this column existed have no node, and a corpus whose documents were parsed
without structure is still a valid corpus. More importantly, a node tree can be
rebuilt — re-parsing replaces it wholesale — and rebuilding structure must never
take the passages, their embeddings, or their extraction records with it.

Revision ID: 008_passage_node
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "008_passage_node"
down_revision = "007_document_nodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "passages",
        sa.Column(
            "node_id",
            sa.Uuid,
            sa.ForeignKey("core.document_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema="core",
    )
    # The reverse direction — "every passage in this section" — is what reading
    # a node actually costs, so it gets the index rather than the forward
    # lookup, which rides the primary key.
    op.create_index("passages_node_idx", "passages", ["node_id"], schema="core")


def downgrade() -> None:
    op.drop_index("passages_node_idx", table_name="passages", schema="core")
    op.drop_column("passages", "node_id", schema="core")
