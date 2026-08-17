"""Postgres passage repository with vector search, FTS, and embedding storage."""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import JSONB
from uuid_utils import uuid7

from research_engine.adapters.storage.postgres.schema import (
    documents,
    entities,
    entity_aliases,
    mentions,
    passage_embeddings,
    passages,
)
from research_engine.domain.errors import UnknownFilterExtension, UnsupportedFilterError
from research_engine.domain.passages import Passage, PassageDraft
from research_engine.services.search.langconfig import is_known_config

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

    from research_engine.domain.filter_extension import FilterExtension
    from research_engine.ports.repositories import Transaction

logger = structlog.get_logger()

#: How long the set of in-corpus language configs is cached. Short, because a
#: document ingested in a new language must become searchable without a restart.
_LANG_CACHE_TTL_SECONDS = 60.0

#: Every key this repository knows how to turn into a WHERE clause.
#:
#: A key absent from this set raises rather than being ignored, which is what
#: makes ``SearchResult.applied_filters`` honest.  A key present here without a
#: real branch below is caught by the reflection test in
#: ``tests/unit/services/test_passage_filters.py``.
SUPPORTED_FILTERS = frozenset(
    {
        "document_types",
        "date_range_start",
        "date_range_end",
        "author_entity_id",
        "recipient_entity_id",
        "mentions_entity_ids",
        "metadata",
        "language",
        "extensions",
        "extension_logic",
    }
)


def validate_filters(
    filters: dict[str, Any],
    filter_extensions: dict[str, FilterExtension] | None = None,
) -> None:
    """Reject filter keys and extension ids that would otherwise be ignored.

    Raises:
        UnsupportedFilterError: a key has no branch in the repository.
        UnknownFilterExtension: an extension was requested but is not registered.
    """
    if unknown := sorted(set(filters) - SUPPORTED_FILTERS):
        raise UnsupportedFilterError(unknown, sorted(SUPPORTED_FILTERS))

    ext_filters = filters.get("extensions") or {}
    if ext_filters:
        available = filter_extensions or {}
        for ext_id in ext_filters:
            if ext_id not in available:
                raise UnknownFilterExtension(ext_id, sorted(available))


def build_candidate_stmt(
    filters: dict[str, Any],
    filter_extensions: dict[str, FilterExtension] | None = None,
    author_names: list[str] | None = None,
    recipient_names: list[str] | None = None,
) -> sa.Select:
    """Build the candidate-id SELECT.

    Pure so that filter translation is unit-testable without a database.  Entity
    name resolution happens in the caller because it needs I/O; the resolved
    names arrive as *author_names* / *recipient_names*.
    """
    stmt = sa.select(passages.c.id).distinct()

    needs_documents = any(
        filters.get(key)
        for key in ("document_types", "date_range_start", "date_range_end", "language")
    ) or author_names is not None or recipient_names is not None

    if needs_documents:
        stmt = stmt.join(documents, documents.c.id == passages.c.document_id)

    if doc_types := filters.get("document_types"):
        stmt = stmt.where(documents.c.document_type.in_(doc_types))

    if date_start := filters.get("date_range_start"):
        stmt = stmt.where(documents.c.created_date_start >= date_start)

    if date_end := filters.get("date_range_end"):
        stmt = stmt.where(documents.c.created_date_end <= date_end)

    if language := filters.get("language"):
        stmt = stmt.where(documents.c.language == language)

    if author_names is not None:
        stmt = stmt.where(_name_matches(documents.c.metadata["author"], author_names))

    if recipient_names is not None:
        stmt = stmt.where(_name_matches(documents.c.metadata["recipient"], recipient_names))

    if entity_ids := filters.get("mentions_entity_ids"):
        for eid in entity_ids:
            subq = sa.select(mentions.c.passage_id).where(mentions.c.entity_id == eid)
            stmt = stmt.where(passages.c.id.in_(subq))

    if metadata_filter := filters.get("metadata"):
        # The column is `json`, not `jsonb`, and `.contains()` on SQLAlchemy's
        # generic JSON type compiles to a string LIKE — which silently matches
        # almost nothing. Cast so this is real containment (`@>`).
        stmt = stmt.where(sa.cast(passages.c.metadata, JSONB).contains(metadata_filter))

    # --- Extension filters ---
    # validate_filters has already guaranteed every requested id resolves.
    ext_filters = filters.get("extensions") or {}
    if ext_filters and filter_extensions:
        extension_logic = filters.get("extension_logic", "and")
        clauses = [
            filter_extensions[ext_id].build_clause(ext_value)
            for ext_id, ext_value in ext_filters.items()
        ]
        if clauses:
            if extension_logic == "or":
                stmt = stmt.where(passages.c.id.in_(sa.union(*clauses)))
            else:
                for clause in clauses:
                    stmt = stmt.where(passages.c.id.in_(clause))

    return stmt


def build_keyword_search_sql(configs: list[str]) -> str:
    """One indexed branch per language config, unioned.

    The obvious form — ``plainto_tsquery(pf.lang_config, :query)`` — is correct
    and unusable: the tsquery varies per row, so ``passage_fts_ts_idx`` cannot be
    used and every search becomes a sequential scan over the FTS table. Building
    one branch per distinct config keeps the query constant within each branch,
    which is what lets the GIN index apply.

    Every config must already be validated by ``is_known_config``: they are
    interpolated as SQL literals because Postgres requires a literal regconfig
    in ``plainto_tsquery``.
    """
    if not configs:
        raise ValueError("build_keyword_search_sql requires at least one config")
    if bad := [c for c in configs if not is_known_config(c)]:
        raise ValueError(f"refusing to interpolate unvalidated regconfig(s): {bad}")

    branches = [
        f"""
        SELECT pf.passage_id, ts_rank_cd(pf.ts, q{i}.tsq) AS kw_score
        FROM core.passage_fts pf, plainto_tsquery('{cfg}', :query) AS q{i}(tsq)
        WHERE pf.lang_config = '{cfg}'::regconfig
          AND pf.ts @@ q{i}.tsq
          AND (:no_filter OR pf.passage_id = ANY(:candidate_ids))
        """
        for i, cfg in enumerate(configs)
    ]
    return "UNION ALL".join(branches) + "\nORDER BY kw_score DESC\nLIMIT :k"


def _name_matches(column: Any, names: list[str]) -> sa.ColumnElement[bool]:
    """Case-insensitive substring match of *column* against any of *names*.

    Interim behaviour for ``author_entity_id`` / ``recipient_entity_id`` — see
    ``PGPassageRepo._resolve_entity_names``.
    """
    if not names:
        return sa.false()
    text_col = sa.func.lower(column.as_string())
    return sa.or_(*[text_col.contains(name.lower()) for name in names])


class PGPassageRepo:
    def __init__(self, engine: AsyncEngine, ef_search: int | None = None) -> None:
        self._engine = engine
        #: HNSW search breadth applied per vector query. None leaves the server
        #: default in place, which is what a database with no HNSW index wants.
        self._ef_search = ef_search
        self._lang_cache: list[str] | None = None
        self._lang_cache_expires: float = 0.0

    async def insert_many(
        self, tx: Transaction, document_id: UUID, drafts: list[PassageDraft]
    ) -> list[Passage]:
        results = []
        for draft in drafts:
            pid = uuid7()
            content_hash = hashlib.sha256(draft.text.encode()).digest()
            values = {
                "id": pid,
                "document_id": document_id,
                "position": draft.position,
                "char_start": draft.char_start,
                "char_end": draft.char_end,
                "locator": draft.locator,
                "text": draft.text,
                "token_count": draft.token_count,
                "chunker": draft.chunker,
                "chunker_version": draft.chunker_version,
                "metadata": draft.metadata,
                "content_hash": content_hash,
                "node_id": draft.node_id,
            }
            await tx.conn.execute(passages.insert().values(**values))
            row = (
                await tx.conn.execute(passages.select().where(passages.c.id == pid))
            ).first()
            results.append(self._to_domain(row))
        return results

    async def relabel_version(
        self,
        tx: Transaction,
        passage_ids: list[UUID],
        chunker_version: str,
        token_counts: dict[UUID, int] | None = None,
    ) -> int:
        """Move passages onto *chunker_version* without touching their text.

        Only for passages a current chunker reproduces byte-identically. Their
        embeddings and FTS rows stay valid precisely because the text did not
        change, which is what makes this cheap: the alternative is deleting and
        re-embedding a quarter of a million passages to correct a label.
        """
        if not passage_ids:
            return 0
        await tx.conn.execute(
            passages.update()
            .where(passages.c.id.in_(passage_ids))
            .values(chunker_version=chunker_version)
        )
        for passage_id, count in (token_counts or {}).items():
            await tx.conn.execute(
                passages.update()
                .where(passages.c.id == passage_id)
                .values(token_count=count)
            )
        return len(passage_ids)

    async def get(self, passage_id: UUID) -> Passage | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(passages.select().where(passages.c.id == passage_id))
            ).first()
            return self._to_domain(row) if row else None

    async def get_by_document(self, document_id: UUID) -> list[Passage]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                passages.select()
                .where(passages.c.document_id == document_id)
                .order_by(passages.c.position)
            )
            return [self._to_domain(row) for row in result]

    async def get_by_node(
        self, node_id: UUID, *, include_descendants: bool = False
    ) -> list[Passage]:
        """Passages inside a node, in document order.

        With *include_descendants*, the node's whole subtree — reading a
        chapter means reading its sections too, and the ltree containment
        operator resolves that in one scan rather than one query per level.
        """
        if include_descendants:
            stmt = sa.text(
                "SELECT p.* FROM core.passages p "
                "JOIN core.document_nodes c ON c.id = p.node_id "
                "JOIN core.document_nodes n ON n.id = :node_id "
                "WHERE c.document_id = n.document_id AND c.path <@ n.path "
                "ORDER BY p.position"
            )
            async with self._engine.connect() as conn:
                rows = await conn.execute(stmt, {"node_id": node_id})
                return [self._to_domain(row) for row in rows]

        async with self._engine.connect() as conn:
            result = await conn.execute(
                passages.select()
                .where(passages.c.node_id == node_id)
                .order_by(passages.c.position)
            )
            return [self._to_domain(row) for row in result]

    async def get_context(
        self, passage_id: UUID, before: int = 0, after: int = 0
    ) -> tuple[list[Passage], Passage, list[Passage]]:
        async with self._engine.connect() as conn:
            # Get target passage
            target_row = (
                await conn.execute(passages.select().where(passages.c.id == passage_id))
            ).first()
            if not target_row:
                raise ValueError(f"Passage not found: {passage_id}")
            target = self._to_domain(target_row)

            before_list = []
            after_list = []

            if before > 0:
                result = await conn.execute(
                    passages.select()
                    .where(
                        sa.and_(
                            passages.c.document_id == target.document_id,
                            passages.c.position < target.position,
                        )
                    )
                    .order_by(passages.c.position.desc())
                    .limit(before)
                )
                before_list = [self._to_domain(r) for r in result]
                before_list.reverse()

            if after > 0:
                result = await conn.execute(
                    passages.select()
                    .where(
                        sa.and_(
                            passages.c.document_id == target.document_id,
                            passages.c.position > target.position,
                        )
                    )
                    .order_by(passages.c.position)
                    .limit(after)
                )
                after_list = [self._to_domain(r) for r in result]

            return before_list, target, after_list

    async def vector_search(
        self,
        query_embedding: list[float],
        model: str,
        model_version: str,
        candidate_ids: list[UUID] | None,
        k: int,
    ) -> list[tuple[UUID, float]]:
        async with self._engine.connect() as conn:
            # SET LOCAL, so the value is scoped to this statement's transaction
            # and cannot leak onto the next borrower of a pooled connection.
            if self._ef_search is not None:
                await conn.execute(
                    sa.text(f"SET LOCAL hnsw.ef_search = {int(self._ef_search)}")
                )
            # Use pgvector cosine distance
            embedding_str = f"[{','.join(str(x) for x in query_embedding)}]"
            sql = sa.text("""
                SELECT pe.passage_id,
                       1 - (pe.embedding <=> CAST(:qv AS vector)) AS vec_score
                FROM core.passage_embeddings pe
                WHERE pe.model = :model AND pe.model_version = :mv
                  AND (:no_filter OR pe.passage_id = ANY(:candidate_ids))
                ORDER BY pe.embedding <=> CAST(:qv AS vector)
                LIMIT :k
            """)
            result = await conn.execute(
                sql,
                {
                    "qv": embedding_str,
                    "model": model,
                    "mv": model_version,
                    "no_filter": candidate_ids is None,
                    "candidate_ids": candidate_ids or [],
                    "k": k,
                },
            )
            return [(row.passage_id, row.vec_score) for row in result]

    async def keyword_search(
        self,
        query: str,
        lang: str | None,
        candidate_ids: list[UUID] | None,
        k: int,
    ) -> list[tuple[UUID, float]]:
        """Rank passages by FTS relevance, stemming each in its own language.

        *lang* is a Postgres regconfig (see ``services.search.langconfig``). When
        it is ``None`` the search spans every language present in the corpus.
        """
        if lang is not None:
            configs = [lang] if is_known_config(lang) else []
        else:
            configs = await self._distinct_lang_configs()

        if not configs:
            return []

        sql = sa.text(build_keyword_search_sql(configs))
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sql,
                {
                    "query": query,
                    "no_filter": candidate_ids is None,
                    "candidate_ids": candidate_ids or [],
                    "k": k,
                },
            )
            return [(row.passage_id, row.kw_score) for row in result]

    async def _distinct_lang_configs(self) -> list[str]:
        """The regconfigs actually present in ``passage_fts``, briefly cached.

        In practice two or three. Cached because it runs on every unfiltered
        search; the TTL is short because ingesting a document in a new language
        must start being searchable without a restart.
        """
        now = time.monotonic()
        if self._lang_cache is not None and now < self._lang_cache_expires:
            return self._lang_cache

        async with self._engine.connect() as conn:
            rows = await conn.execute(
                sa.text("SELECT DISTINCT lang_config::text AS cfg FROM core.passage_fts")
            )
            configs = [row.cfg for row in rows]

        unknown = [c for c in configs if not is_known_config(c)]
        if unknown:
            # Someone indexed under a config this build does not vouch for.
            # Skipping it loses recall for that language; interpolating it into
            # SQL is worse.
            logger.warning("unknown_lang_config_skipped", configs=unknown)

        self._lang_cache = sorted(c for c in configs if is_known_config(c))
        self._lang_cache_expires = now + _LANG_CACHE_TTL_SECONDS
        return self._lang_cache

    async def store_embeddings(
        self,
        tx: Transaction,
        passage_ids: list[UUID],
        embeddings: list[list[float]],
        model: str,
        model_version: str,
        dim: int,
    ) -> None:
        for pid, emb in zip(passage_ids, embeddings, strict=False):
            await tx.conn.execute(
                passage_embeddings.insert().values(
                    passage_id=pid,
                    model=model,
                    model_version=model_version,
                    dim=dim,
                    embedding=emb,
                )
            )

    async def index_fts(
        self, tx: Transaction, passage_ids: list[UUID], texts: list[str], lang: str
    ) -> None:
        """Index passage text under the Postgres regconfig *lang*.

        The upsert refreshes ``lang_config`` as well as ``ts``: updating only the
        vector would leave the column describing a stemming that no longer
        applies, and ``keyword_search`` routes on that column.
        """
        if not is_known_config(lang):
            raise ValueError(f"Unknown text-search config: {lang!r}")

        for pid, text in zip(passage_ids, texts, strict=False):
            await tx.conn.execute(
                # CAST(... AS regconfig), not `::regconfig`: sa.text() reads `::`
                # as part of the bind-parameter name and leaves it unsubstituted.
                sa.text("""
                    INSERT INTO core.passage_fts (passage_id, lang_config, ts)
                    VALUES (
                        :pid,
                        CAST(:lang AS regconfig),
                        to_tsvector(CAST(:lang AS regconfig), :text)
                    )
                    ON CONFLICT (passage_id) DO UPDATE
                        SET lang_config = EXCLUDED.lang_config,
                            ts = EXCLUDED.ts
                """),
                {"pid": pid, "lang": lang, "text": text},
            )
        self._lang_cache = None

    async def get_embedding(
        self, passage_id: UUID, model: str, model_version: str
    ) -> list[float] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    passage_embeddings.select().where(
                        sa.and_(
                            passage_embeddings.c.passage_id == passage_id,
                            passage_embeddings.c.model == model,
                            passage_embeddings.c.model_version == model_version,
                        )
                    )
                )
            ).first()
            if row:
                return list(row.embedding)
            return None

    async def filter_candidate_ids(
        self,
        filters: dict[str, Any],
        filter_extensions: dict[str, FilterExtension] | None = None,
    ) -> list[UUID]:
        """Build candidate passage ID set from filters.

        *filter_extensions* maps extension IDs to ``FilterExtension``
        instances.  Extension filters referenced in ``filters["extensions"]``
        are resolved via these instances and AND/OR-composed per
        ``filters["extension_logic"]``.

        Raises ``UnsupportedFilterError`` for any key this repository cannot
        translate, so a filter that reaches the query is a filter that ran.
        """
        validate_filters(filters, filter_extensions)

        author_names = await self._resolve_entity_names(filters.get("author_entity_id"))
        recipient_names = await self._resolve_entity_names(filters.get("recipient_entity_id"))

        stmt = build_candidate_stmt(filters, filter_extensions, author_names, recipient_names)

        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return [row[0] for row in result]

    async def _resolve_entity_names(self, entity_id: UUID | None) -> list[str] | None:
        """Resolve an entity to its canonical name plus aliases.

        Interim implementation of ``author_entity_id`` / ``recipient_entity_id``.
        There is no ingest-time author-to-entity link yet, so this degrades to a
        *name* match against ``documents.metadata``, not an *identity* match: it
        will miss documents whose recorded author string differs from every known
        alias, and can over-match on a shared surname.

        P3-6 replaces this with a real join through ``bib_contributors.entity_id``,
        at which point this method and its warning go away.
        """
        if entity_id is None:
            return None

        stmt = (
            sa.select(entities.c.canonical_name.label("name"))
            .where(entities.c.id == entity_id)
            .union(
                sa.select(entity_aliases.c.alias.label("name")).where(
                    entity_aliases.c.entity_id == entity_id
                )
            )
        )
        async with self._engine.connect() as conn:
            names = [row.name for row in await conn.execute(stmt) if row.name]

        logger.warning(
            "author_filter_name_match",
            entity_id=str(entity_id),
            names=names,
            detail=(
                "Matching authorship by name against documents.metadata; this is a "
                "name match, not an identity match. Replaced by a bib_contributors "
                "join in P3-6."
            ),
        )
        return names

    async def count(self) -> int:
        async with self._engine.connect() as conn:
            return (await conn.execute(sa.select(sa.func.count()).select_from(passages))).scalar_one()

    @staticmethod
    def _to_domain(row: Any) -> Passage:
        return Passage(
            id=row.id,
            document_id=row.document_id,
            position=row.position,
            char_start=row.char_start,
            char_end=row.char_end,
            locator=row.locator or {},
            text=row.text,
            token_count=row.token_count,
            chunker=row.chunker,
            chunker_version=row.chunker_version,
            metadata=row.metadata or {},
            node_id=row.node_id,
            content_hash=bytes(row.content_hash),
            created_at=row.created_at,
        )
