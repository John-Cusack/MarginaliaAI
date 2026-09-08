"""Index `core.words` by `(language, strong)`, not by `strong` alone.

A Strong's number is only unique inside its lexicon. `4941` is Hebrew
*mishpat*; `G4941` is a different word in a different language, and the column
stores bare digits with nothing to tell them apart. Today every row is `he`, so
the collision is latent — which is exactly when it is cheap to close. Loading
any Greek would make `strong = '4941'` silently return two lexicons' worth of
occurrences, recoverable only by remembering a filter nothing enforced.

The composite index is the enforcement that survives forgetting: every query
that reaches `core.words` by Strong's number now has an index that only pays off
if it also names a language, so the natural query shape is the correct one.
`words_strong_idx` stays, because `lemma`-first lookups and language-agnostic
counts still use it and it costs 4 MB.

Additive: one index, no column changes, no rewrite of the 305,517 rows.

Revision ID: 015_words_language_strong
Create Date: 2026-09-08
"""

from alembic import op

revision = "015_words_language_strong"
down_revision = "014_words"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "words_language_strong_idx",
        "words",
        ["language", "strong"],
        schema="core",
    )


def downgrade() -> None:
    op.drop_index("words_language_strong_idx", table_name="words", schema="core")
