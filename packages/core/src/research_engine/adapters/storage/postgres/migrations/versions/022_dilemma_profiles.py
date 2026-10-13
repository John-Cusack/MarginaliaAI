"""Reader profiles, the roles they give editions, and the runs made under them.

A text's standing is not a property of the text. The King James Bible is
scripture to one reader, an outside text a second reader's scripture names, and
one witness among several to a third. So standing belongs to a **profile** — a
reader's declared view of the library — and the engine stores each text once:

- **`dilemma.profiles`**: one row per profile key, with the hash of the profile
  data last loaded, so every run can say exactly which declaration it ran under.
- **`dilemma.source_roles`**: what an edition is *to that profile* — `binding`,
  `external`, `reference`, `defense`, `critical` or `fact` — at what tier, over
  what dates, and optionally for part of the edition only (`scope`, a locator
  filter such as `{"books": [...]}`). One edition may hold several roles at
  once (an essay both binding and a defense), but never the same role twice
  over overlapping dates and scope.
- **`dilemma.runs`**: the versioning spine. Every derived row will point at the
  run that made it, and the run records the profile hash, config and its hash,
  model versions, counts in and out, and the cost the gate estimated. What was
  actually spent stays in `core.llm_calls`, the one ledger of model calls.

The role vocabulary is the engine's own, not a tradition's: Stage 0 of the
contradiction funnel decides what may be a horn from it. Everything a tradition
means lives in the profile data, outside the schema.

Additive: no existing table changes.

Revision ID: 022_dilemma_profiles
Revises: 021_provenance
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import DATERANGE, JSONB

revision = "022_dilemma_profiles"
down_revision = "021_provenance"
branch_labels = None
depends_on = None

ROLES = ("binding", "external", "reference", "defense", "critical", "fact")


def upgrade() -> None:
    # Equality on uuid/text inside a GiST exclusion constraint needs btree_gist.
    # It is a trusted extension, so the database owner can create it.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE SCHEMA IF NOT EXISTS dilemma")

    op.create_table(
        "profiles",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("key", sa.Text, nullable=False, unique=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("profile_hash", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("settings", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "loaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("key ~ '^[a-z][a-z0-9_]*$'", name="profiles_key_ck"),
        sa.CheckConstraint("version >= 1", name="profiles_version_ck"),
        sa.CheckConstraint(
            "profile_hash ~ '^[0-9a-f]{64}$'", name="profiles_hash_ck"
        ),
        schema="dilemma",
    )

    op.create_table(
        "source_roles",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "profile_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "edition_id",
            sa.Uuid,
            sa.ForeignKey("bibliography.editions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("tier", sa.SmallInteger),
        sa.Column(
            "validity", DATERANGE, nullable=False, server_default=sa.text("'(,)'")
        ),
        sa.Column("scope", JSONB, nullable=False, server_default="{}"),
        sa.Column("notes", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN (" + ", ".join(f"'{r}'" for r in ROLES) + ")",
            name="source_roles_role_ck",
        ),
        sa.CheckConstraint("tier BETWEEN 1 AND 9", name="source_roles_tier_ck"),
        sa.CheckConstraint("NOT isempty(validity)", name="source_roles_validity_ck"),
        schema="dilemma",
    )
    # One role of a kind per edition, profile and scope at any date. Scope is
    # compared by its canonical jsonb text, so `{}` (the whole edition) collides
    # with `{}`, and two different book lists do not collide with each other.
    op.execute(
        "ALTER TABLE dilemma.source_roles "
        "ADD CONSTRAINT source_roles_no_overlap EXCLUDE USING gist ("
        "profile_id WITH =, edition_id WITH =, role WITH =, "
        "(md5(scope::text)) WITH =, validity WITH &&)"
    )
    op.create_index(
        "source_roles_edition_idx", "source_roles", ["edition_id"], schema="dilemma"
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "profile_id",
            sa.Uuid,
            sa.ForeignKey("dilemma.profiles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="running"),
        sa.Column("profile_hash", sa.Text, nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default="{}"),
        sa.Column("config_hash", sa.Text, nullable=False),
        sa.Column("code_version", sa.Text),
        sa.Column("model_versions", JSONB, nullable=False, server_default="{}"),
        sa.Column("items_in", sa.Integer),
        sa.Column("items_out", sa.Integer),
        sa.Column("cost_estimate", sa.Numeric(12, 4)),
        sa.Column("stats", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running','finished','failed')", name="runs_status_ck"
        ),
        # A run is finished exactly when it says when it finished.
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="runs_finished_ck"
        ),
        schema="dilemma",
    )
    op.create_index(
        "runs_profile_stage_idx",
        "runs",
        ["profile_id", "stage", "started_at"],
        schema="dilemma",
    )


def downgrade() -> None:
    op.drop_table("runs", schema="dilemma")
    op.drop_table("source_roles", schema="dilemma")
    op.drop_table("profiles", schema="dilemma")
    op.execute("DROP SCHEMA IF EXISTS dilemma")
    # btree_gist stays: another object may have come to rely on it, and leaving
    # an unused extension installed is harmless.
