"""Add `core.words`: the morphological analysis of a source text, word by word.

A pointed Hebrew word has no searchable form. `mishpat` is written 205 distinct
ways across the Westminster Leningrad Codex — inflected, pointed, accented, and
carrying prefixed conjunctions, articles and prepositions — so the question a
lexicographic survey exists to ask, "where does this word occur", cannot be put
to the text at all. Measured on this corpus: the commonest single spelling finds
31 of 422 occurrences, and the unpointed consonantal form finds none, because
the vowel points sit between the letters.

The analysis already exists in the sources; it was simply never stored. This
table holds it, addressed the way passages and nodes already are — a document
and a span into that document's canonical text — so a word, the passage that
retrieved it, and the verse that contains it are all the same kind of address
and join without a new concept.

One row per word of the running text, never per morpheme. That is what makes
the index checkable: each row's span must quote its own word exactly, and once
that holds everywhere, any word missing from the table leaves its letters in a
stretch of text no row claims. The loader refuses to write unless both hold.

Additive: no existing table changes, so nothing is re-chunked and nothing is
re-embedded.

Revision ID: 014_words
Create Date: 2026-09-08
"""

import sqlalchemy as sa
from alembic import op

revision = "014_words"
down_revision = "013_edition_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "words",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            sa.Uuid,
            sa.ForeignKey("core.documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("char_start", sa.Integer, nullable=False),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("surface", sa.Text, nullable=False),
        sa.Column("lemma", sa.Text, nullable=False),
        sa.Column("strong", sa.Text),
        sa.Column("homograph", sa.Text),
        sa.Column("prefixes", sa.Text),
        sa.Column("morph", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("ref", sa.Text, nullable=False),
        sa.Column("from_qere", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("document_id", "position"),
        sa.CheckConstraint("char_end > char_start", name="words_span_is_forward"),
        schema="core",
    )
    op.create_index("words_strong_idx", "words", ["strong"], schema="core")
    op.create_index("words_lemma_idx", "words", ["lemma"], schema="core")
    op.create_index("words_document_idx", "words", ["document_id"], schema="core")
    op.create_index(
        "words_doc_span_idx",
        "words",
        ["document_id", "char_start", "char_end"],
        schema="core",
    )


def downgrade() -> None:
    op.drop_table("words", schema="core")
