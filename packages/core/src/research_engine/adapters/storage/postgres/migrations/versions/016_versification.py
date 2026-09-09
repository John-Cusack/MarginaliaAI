"""Versification and book identity as data: three tables, no rules in code.

Two defects made a cross-edition join over this corpus quietly wrong, and they
compound, so they are repaired together.

**Book identity.** LHB and WLC address Ecclesiastes, Hosea, Micah and Nahum as
`ECC HO MIC NAH`; ESV addresses them as `EC HOS MI NA`. A join on the article
code therefore returns *silence, not error* for four of the thirty-nine Old
Testament books — and those four are among the versification-divergent ones, so
the second defect hid inside the first. Measured here: joining LHB to ESV on the
raw code finds 23 books whose verse counts diverge; repairing the codes finds
**27**, which is exactly the number of books `wlc/VerseMap.xml` maps. That
agreement is the check that the repair is complete, and it is asserted in the
integration suite rather than left as a note.

**Versification.** The Hebrew and English traditions disagree about where 1,978
verses sit. The map was on disk in the WLC source all along
(`wlc/VerseMap.xml`) and the ingest dropped it. It is loaded here rather than
reimplemented, because a rule set that reproduces it would be a second source of
truth for something already written down.

Three deliberate shapes:

* `editions_versification` names a *scheme* per edition, not per book. An
  edition follows one tradition; which one is the fact worth storing.
* `edition_books` maps `(edition, code)` to an OSIS book id. Book identity is
  data because the corpus already holds three disagreeing vocabularies, and a
  fourth arrives with every new edition.
* `verse_map` is a **pair table, not an offset column.** Seven of the 1,978
  mappings are `type="partial"` — a verse that begins midway through another,
  written `Isa.63.19!b` — and no integer offset can express one. A schema with
  an `offset` column would have had to round them, silently. Here a partial
  carries its `!a`/`!b` part explicitly and a reader that cannot handle parts
  can see that it cannot.

Additive: three new tables, nothing existing altered.

Revision ID: 016_versification
Create Date: 2026-09-08
"""

import sqlalchemy as sa
from alembic import op

revision = "016_versification"
down_revision = "015_words_language_strong"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "editions_versification",
        sa.Column("edition_key", sa.Text, primary_key=True),
        # 'hebrew' or 'english' today. Free text rather than an enum because a
        # third tradition (LXX/Vulgate numbering) is a data load, not a migration.
        sa.Column("scheme", sa.Text, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        schema="core",
    )

    op.create_table(
        "edition_books",
        sa.Column("edition_key", sa.Text, nullable=False),
        # The code this edition writes in `documents.metadata->>'article'`.
        sa.Column("code", sa.Text, nullable=False),
        # The OSIS book id every edition is joined through: "Eccl", "Hos".
        sa.Column("osis_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text),
        # Canonical position, so "order by book" is a column rather than a
        # hardcoded list of sixty-six names somewhere in core.
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("edition_key", "code"),
        schema="core",
    )
    op.create_index(
        "edition_books_osis_idx",
        "edition_books",
        ["edition_key", "osis_id"],
        unique=True,
        schema="core",
    )

    op.create_table(
        "verse_map",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("from_scheme", sa.Text, nullable=False),
        sa.Column("to_scheme", sa.Text, nullable=False),
        # OSIS references, "Isa.63.19", matching `core.words.ref` exactly.
        sa.Column("from_ref", sa.Text, nullable=False),
        sa.Column("to_ref", sa.Text, nullable=False),
        # 'a' or 'b' where the source marks a half-verse, else NULL. A row with
        # either part set is a partial and must not be read as a whole-verse
        # equivalence.
        sa.Column("from_part", sa.Text),
        sa.Column("to_part", sa.Text),
        sa.Column("mapping_type", sa.Text, nullable=False),
        # Where the row came from, so a reload can tell its own rows apart from
        # another map's: "morphhb@3d15126 wlc/VerseMap.xml".
        sa.Column("source", sa.Text, nullable=False),
        sa.CheckConstraint(
            "mapping_type IN ('full', 'partial')", name="verse_map_type_known"
        ),
        sa.CheckConstraint(
            "(from_part IS NULL AND to_part IS NULL) OR mapping_type = 'partial'",
            name="verse_map_parts_are_partial",
        ),
        schema="core",
    )
    op.create_index(
        "verse_map_from_idx",
        "verse_map",
        ["from_scheme", "to_scheme", "from_ref"],
        schema="core",
    )
    op.create_index(
        "verse_map_to_idx",
        "verse_map",
        ["to_scheme", "from_scheme", "to_ref"],
        schema="core",
    )


def downgrade() -> None:
    op.drop_table("verse_map", schema="core")
    op.drop_table("edition_books", schema="core")
    op.drop_table("editions_versification", schema="core")
