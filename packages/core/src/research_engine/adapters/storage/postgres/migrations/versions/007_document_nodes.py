"""Store the document's structural tree alongside its passages.

Passages answer "what are the retrievable fragments"; nodes answer "what are the
parts the author wrote". Both address `document_texts.text` by span and neither
copies it, so a document carries one substrate with two indexes over it.

`path` is an `ltree`, which makes "everything under chapter 4" a single GiST
index scan instead of a recursive CTE. The extension is created here rather than
assumed: `vector` and `pg_trgm` were created the same way in 001.

The GiST index on `path` is what earns the extension — a btree on `path` would
serve equality and prefix but not the `<@` / `@>` containment operators that
subtree navigation is built on.

Revision ID: 007_document_nodes
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "007_document_nodes"
down_revision = "006_vector_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree")

    op.create_table(
        "document_nodes",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Self-referential and cascading: dropping a chapter drops its sections,
        # which is the only sane reading of a containment tree.
        sa.Column(
            "parent_id",
            sa.Uuid,
            sa.ForeignKey("core.document_nodes.id", ondelete="CASCADE"),
        ),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("depth", sa.Integer, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("node_type", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        # Span into the canonical text. NOT NULL, unlike `passages.char_start`:
        # nodes are new, so there is no pre-offsets generation to accommodate,
        # and a node without a span could not be read back at all.
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("metadata", sa.JSON, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.CheckConstraint("char_end >= char_start", name="document_nodes_span_ck"),
        sa.UniqueConstraint("document_id", "path"),
        schema="core",
    )

    # `path` is declared Text above so Alembic emits a column it understands,
    # then retyped: ltree's input parser rejects malformed labels, which is a
    # constraint worth having rather than discovering at query time.
    op.execute("ALTER TABLE core.document_nodes ALTER COLUMN path TYPE ltree USING path::ltree")

    op.create_index(
        "document_nodes_document_idx", "document_nodes", ["document_id"], schema="core"
    )
    op.create_index(
        "document_nodes_parent_idx", "document_nodes", ["parent_id"], schema="core"
    )
    # Containment lookups: "which node holds this passage", probed by span
    # within one document. The hot path joining the passage layer to the tree.
    op.create_index(
        "document_nodes_span_idx",
        "document_nodes",
        ["document_id", "char_start", "char_end"],
        schema="core",
    )
    op.execute(
        "CREATE INDEX document_nodes_path_gist "
        "ON core.document_nodes USING gist (path)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.document_nodes_path_gist")
    op.drop_index("document_nodes_span_idx", table_name="document_nodes", schema="core")
    op.drop_index("document_nodes_parent_idx", table_name="document_nodes", schema="core")
    op.drop_index(
        "document_nodes_document_idx", table_name="document_nodes", schema="core"
    )
    op.drop_table("document_nodes", schema="core")
    # The extension is left in place: another table may have come to depend on
    # it, and dropping it would take their columns with it.
