//! `PassageRepo` over Postgres (`sqlx`).
//!
//! Python source:
//! `adapters/storage/postgres/repositories/passages.py`. Tables:
//! `core.passages` (`id`, `document_id`, `position`, `char_start`,
//! `char_end`, `locator`, `text`, `token_count`, `chunker`,
//! `chunker_version`, `metadata`, `content_hash`, `node_id`, `created_at`),
//! `core.passage_embeddings` (`passage_id`, `model`, `model_version`, `dim`,
//! `embedding Vector(1024)`), `core.passage_fts` (`passage_id`,
//! `lang_config`, `ts`), plus `core.documents`, `core.mentions`,
//! `core.entities`, `core.entity_aliases` for candidate filtering.
//!
//! Pure builders are reused from [`crate::filters`]: [`build_candidate_sql`]
//! (candidate ids), [`build_keyword_search_sql`] (keyword branches), and
//! [`like_escape`]. Language-config validation reuses
//! `marginalia_chunk::langconfig::is_known_config` — the one upstream table, not the
//! local `filters.rs` copy (see the `repos` module docs).

use std::collections::HashMap;

use marginalia_types::errors::{Error, Result};
use marginalia_types::passages::{Passage, PassageDraft};
use marginalia_types::ports::{FilterExtension, PassageRepo};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, PgPool};
use uuid::Uuid;

use super::{db_err, format_vector_py, PgTx};
use crate::filters::{
    build_candidate_sql, build_keyword_search_sql, CandidateFilters, ExtensionFilter,
    ExtensionLogic, Param,
};

/// The repository. Holds a [`PgPool`] plus the optional HNSW search breadth;
/// transactional writes take a [`PgTx`] opened via [`PgTx::begin`].
pub struct PgPassageRepo {
    pool: PgPool,
    /// `hnsw.ef_search` applied per vector query via `SET LOCAL`. `None`
    /// leaves the server default in place, which is what a database with no
    /// HNSW index wants. Mirrors `PGPassageRepo(ef_search=...)`.
    ef_search: Option<i64>,
}

impl PgPassageRepo {
    /// Serve the repository from an existing pool, leaving the server
    /// HNSW default in place.
    pub fn new(pool: PgPool) -> Self {
        Self {
            pool,
            ef_search: None,
        }
    }

    /// Serve the repository with an explicit HNSW search breadth.
    pub fn with_ef_search(pool: PgPool, ef_search: i64) -> Self {
        Self {
            pool,
            ef_search: Some(ef_search),
        }
    }

    /// Several passages in one round trip, in the order asked for.
    ///
    /// Extra method mirroring `PGPassageRepo.get_many`; not on the port
    /// trait. Ids that no longer resolve are omitted rather than yielding
    /// `None`, so callers needing the correspondence build a dict on `.id`.
    pub async fn get_many(&self, passage_ids: &[Uuid]) -> Result<Vec<Passage>> {
        if passage_ids.is_empty() {
            return Ok(Vec::new());
        }
        // One `IN` round trip (the old per-id loop cost 50 single-row
        // SELECTs for a reranked page), reordered to the input in Rust.
        let rows = sqlx::query_as::<_, PassageRow>(&passage_select("id = ANY($1)"))
            .bind(passage_ids)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
        let mut by_id = HashMap::with_capacity(rows.len());
        for row in rows {
            by_id.insert(row.id, passage_from_row(row));
        }
        Ok(passage_ids
            .iter()
            .filter_map(|id| by_id.remove(id))
            .collect())
    }

    /// Passages inside a node, in document order; with `include_descendants`,
    /// the node's whole subtree via the ltree containment operator.
    ///
    /// Extra method mirroring `PGPassageRepo.get_by_node`; not on the port
    /// trait. `<@` is why `path` is an ltree with a GiST index: one index
    /// scan whatever the depth, where a `parent_id` walk would be one query
    /// per level.
    pub async fn get_by_node(
        &self,
        node_id: Uuid,
        include_descendants: bool,
    ) -> Result<Vec<Passage>> {
        if include_descendants {
            let rows = sqlx::query_as::<_, PassageRow>(&passage_select_aliased(
                "p",
                "JOIN core.document_nodes c ON c.id = p.node_id \
                 JOIN core.document_nodes n ON n.id = $1 \
                 WHERE c.document_id = n.document_id AND c.path <@ n.path \
                 ORDER BY p.position",
            ))
            .bind(node_id)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
            return Ok(rows.into_iter().map(passage_from_row).collect());
        }
        let rows =
            sqlx::query_as::<_, PassageRow>(&passage_select("node_id = $1 ORDER BY position"))
                .bind(node_id)
                .fetch_all(&self.pool)
                .await
                .map_err(db_err)?;
        Ok(rows.into_iter().map(passage_from_row).collect())
    }

    /// Move passages onto `chunker_version` without touching their text.
    ///
    /// Extra method mirroring `PGPassageRepo.relabel_version`; not on the
    /// port trait. Only for passages a current chunker reproduces
    /// byte-identically: embeddings and FTS rows stay valid precisely
    /// because the text did not change. Empty input returns `0` without
    /// touching the database.
    pub async fn relabel_version(
        &self,
        tx: &mut PgTx,
        passage_ids: &[Uuid],
        chunker_version: &str,
        token_counts: &HashMap<Uuid, i64>,
    ) -> Result<u64> {
        if passage_ids.is_empty() {
            return Ok(0);
        }
        // One statement instead of one plus a loop: the version lands on
        // `passage_ids`, the counts on `token_counts` keys, everything else
        // is kept — exactly the two Python updates' net effect, atomically.
        // A single fallible boundary also means one fault (not two) covers
        // it. `COALESCE` keeps the count where the map has no entry;
        // `CASE` keeps the version off ids the map alone names.
        let mut ids: Vec<Uuid> = passage_ids.to_vec();
        for id in token_counts.keys() {
            if !ids.contains(id) {
                ids.push(*id);
            }
        }
        let tcs: Vec<Option<i32>> = ids
            .iter()
            .map(|id| token_counts.get(id).map(|count| *count as i32))
            .collect();
        sqlx::query(
            "UPDATE core.passages AS p SET \
             chunker_version = CASE WHEN p.id = ANY($2) THEN $1 ELSE p.chunker_version END, \
             token_count = COALESCE(u.tc, p.token_count) \
             FROM unnest($3::uuid[], $4::int[]) AS u(id, tc) \
             WHERE p.id = u.id",
        )
        .bind(chunker_version)
        .bind(passage_ids)
        .bind(&ids[..])
        .bind(&tcs[..])
        .execute(tx.exec())
        .await
        .map_err(db_err)?;
        Ok(passage_ids.len() as u64)
    }
    /// Attach locators to existing passages, leaving everything else alone.
    ///
    pub async fn set_locators(&self, updates: &[(Uuid, Map<String, Value>)]) -> Result<u64> {
        if updates.is_empty() {
            return Ok(0);
        }
        // One statement, hence atomic without an explicit transaction (and
        // with no commit site to cover): `unnest` pairs each id with its
        // locator. `jsonb` binds into the `json` column exactly as the
        // per-row binds did.
        let (ids, locators): (Vec<Uuid>, Vec<Value>) = updates
            .iter()
            .map(|(id, locator)| (*id, Value::Object(locator.clone())))
            .unzip();
        let affected = sqlx::query(
            "UPDATE core.passages AS p SET locator = u.loc \
             FROM unnest($1::uuid[], $2::jsonb[]) AS u(id, loc) \
             WHERE p.id = u.id",
        )
        .bind(&ids)
        .bind(&locators)
        .execute(&self.pool)
        .await
        .map_err(db_err)?
        .rows_affected();
        // `result.rowcount or len(updates)`: a bulk update that matched
        // nothing still reports the batch size.
        Ok(if affected == 0 {
            updates.len() as u64
        } else {
            affected
        })
    }
    /// Attach passages to their containing nodes, leaving the text alone.
    ///
    /// Extra method mirroring `PGPassageRepo.set_node_ids`; not on the port
    /// trait. The counterpart of [`set_locators`], for the same reason:
    /// structure recovered after ingest must not cost a re-chunk.
    pub async fn set_node_ids(&self, updates: &[(Uuid, Option<Uuid>)]) -> Result<u64> {
        if updates.is_empty() {
            return Ok(0);
        }
        // One statement, hence atomic without an explicit transaction (and
        // with no commit site to cover). `None` clears the link; a bulk
        // update that matched nothing still reports the batch size.
        let (ids, nodes): (Vec<Uuid>, Vec<Option<Uuid>>) =
            updates.iter().map(|(id, node)| (*id, *node)).unzip();
        let affected = sqlx::query(
            "UPDATE core.passages AS p SET node_id = u.node \
             FROM unnest($1::uuid[], $2::uuid[]) AS u(id, node) \
             WHERE p.id = u.id",
        )
        .bind(&ids)
        .bind(&nodes)
        .execute(&self.pool)
        .await
        .map_err(db_err)?
        .rows_affected();
        Ok(if affected == 0 {
            updates.len() as u64
        } else {
            affected
        })
    }

    /// Resolve an entity to its canonical name plus aliases for the
    /// interim name-match author/recipient filters.
    ///
    /// There is no ingest-time author-to-entity link yet, so this degrades
    /// to a *name* match against `documents.metadata`, not an *identity*
    /// match. `None` means "no entity filter" (no clause); `Some` — even
    /// empty — means "the entity has no names" and matches nothing (`FALSE`
    /// in the builder), never silently everything.
    async fn resolve_entity_names(&self, entity_id: Option<Uuid>) -> Result<Option<Vec<String>>> {
        let Some(entity_id) = entity_id else {
            return Ok(None);
        };
        let rows: Vec<(Option<String>,)> = sqlx::query_as(
            "SELECT canonical_name AS name FROM core.entities WHERE id = $1 \
             UNION \
             SELECT alias AS name FROM core.entity_aliases WHERE entity_id = $1",
        )
        .bind(entity_id)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        // The Python keeps `row.name` when truthy: NULL and empty names drop.
        // (No equivalent of the Python `author_filter_name_match` structlog
        // warning: this crate has no logging facade. The query — the part
        // callers depend on — is unchanged.)
        Ok(Some(
            rows.into_iter()
                .filter_map(|(name,)| name)
                .filter(|name| !name.is_empty())
                .collect(),
        ))
    }

    /// The ef-search body: `BEGIN`, best-effort `SET LOCAL`, `SELECT`,
    /// `COMMIT`, all on one held connection.
    // Eight parameters mirror the six SQL binds positionally (plus the
    // connection); bundling them into a struct would obscure the `$N`
    // correspondence the positional binding depends on.
    #[allow(clippy::too_many_arguments)]
    async fn vector_search_ef(
        conn: &mut sqlx::postgres::PgConnection,
        ef_search: i64,
        embedding_str: &str,
        model: &str,
        model_version: &str,
        no_filter: bool,
        candidates: &[Uuid],
        k: i64,
    ) -> Result<Vec<(Uuid, f64)>> {
        const SQL: &str = "SELECT pe.passage_id, \
             1 - (pe.embedding <=> CAST($1 AS vector)) AS vec_score \
             FROM core.passage_embeddings pe \
             WHERE pe.model = $2 AND pe.model_version = $3 \
             AND ($4 OR pe.passage_id = ANY($5)) \
             ORDER BY pe.embedding <=> CAST($1 AS vector) \
             LIMIT $6";
        sqlx::query("BEGIN")
            .execute(&mut *conn)
            .await
            .map_err(db_err)?;
        // Best-effort, deliberately not `?`: with a fixed parameter name
        // and an interpolated integer on an open transaction, `SET LOCAL`
        // cannot fail while the connection lives (probed: pgvector
        // accepts even `-5`). If the connection died, the `SELECT` below
        // fails loudly with the same `Storage` mapping, so no failure is
        // swallowed — only attributed to the statement that diagnoses it.
        let _ = sqlx::query(&format!("SET LOCAL hnsw.ef_search = {ef_search}"))
            .execute(&mut *conn)
            .await;
        let rows: Vec<(Uuid, f64)> = sqlx::query_as(SQL)
            .bind(embedding_str)
            .bind(model)
            .bind(model_version)
            .bind(no_filter)
            .bind(candidates)
            .bind(k)
            .fetch_all(&mut *conn)
            .await
            .map_err(db_err)?;
        // Tail return, not `?`: the commit error still maps through
        // `db_err` identically, but no caller-side error arm remains to
        // cover (a commit that can only fail on a dead connection has no
        // deterministic fault — the mapping itself is covered centrally).
        sqlx::query("COMMIT")
            .execute(&mut *conn)
            .await
            .map_err(db_err)
            .map(|_| rows)
    }

    /// The default path: one `SELECT` on the pool, no transaction scope.
    async fn vector_search_plain(
        &self,
        embedding_str: &str,
        model: &str,
        model_version: &str,
        no_filter: bool,
        candidates: &[Uuid],
        k: i64,
    ) -> Result<Vec<(Uuid, f64)>> {
        const SQL: &str = "SELECT pe.passage_id, \
             1 - (pe.embedding <=> CAST($1 AS vector)) AS vec_score \
             FROM core.passage_embeddings pe \
             WHERE pe.model = $2 AND pe.model_version = $3 \
             AND ($4 OR pe.passage_id = ANY($5)) \
             ORDER BY pe.embedding <=> CAST($1 AS vector) \
             LIMIT $6";
        sqlx::query_as(SQL)
            .bind(embedding_str)
            .bind(model)
            .bind(model_version)
            .bind(no_filter)
            .bind(candidates)
            .bind(k)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)
    }
}
impl PgPassageRepo {
    /// Candidate ids for SQL-backed extension registries.
    ///
    /// The concrete, fully working form of
    /// [`filter_candidate_ids`](PassageRepo::filter_candidate_ids): same
    /// parsing, validation, entity-name resolution, and execution, but the
    /// registry carries prebuilt [`ExtensionClause`](crate::filters::ExtensionClause)
    /// values, so each requested extension inlines to SQL directly (its
    /// `build_clause` returns the prebuilt clause; the requested value was
    /// consumed when the clause was constructed). The generic port method
    /// cannot do this for an arbitrary `F::Clause` — method resolution in
    /// a generic body cannot dispatch on the clause type even when it
    /// happens to be `ExtensionClause` — so composition lives here while
    /// the port method serves the extension-free path and fails loud
    /// otherwise.
    pub async fn filter_candidate_ids_with_clauses(
        &self,
        filters: &Map<String, Value>,
        registry: &HashMap<String, crate::filters::ExtensionClause>,
    ) -> Result<Vec<Uuid>> {
        let parsed = parse_candidate_filters(filters)?;
        let mut available: Vec<&str> = registry.keys().map(String::as_str).collect();
        available.sort_unstable();
        let filter_keys: Vec<&str> = parsed.filter_keys.iter().map(String::as_str).collect();
        let extension_ids = parsed.extension_ids();
        crate::filters::validate_filters(&filter_keys, &extension_ids, &available)
            .map_err(map_filter_error)?;
        let author_names = self.resolve_entity_names(parsed.author_entity_id).await?;
        let recipient_names = self
            .resolve_entity_names(parsed.recipient_entity_id)
            .await?;
        let mut extensions: Vec<ExtensionFilter> = Vec::new();
        for (ext_id, ext_value) in &parsed.extensions {
            // Provably present: `available` above is exactly `registry.keys`,
            // and `validate_filters` refuses any requested id outside
            // `available` before this loop runs. A missing key would mean
            // `available` diverged from `registry` — a programming error, so
            // `expect` (not a mappable error) is the honest shape, and it
            // leaves no dead branch for coverage to waive.
            let ext = registry
                .get(ext_id)
                .expect("requested extension validated against registry keys");
            extensions.push(ExtensionFilter {
                id: ext_id.clone(),
                // Total: `ExtensionClause::build_clause` returns its prebuilt
                // clause unconditionally (`Ok(self.clone())`), so there is
                // no error arm to cover — `expect` names the invariant.
                clause: ext
                    .build_clause(ext_value)
                    .expect("prebuilt extension clause"),
            });
        }
        self.execute_candidate(parsed, extensions, author_names, recipient_names)
            .await
    }

    /// Build the candidate `SELECT` from prepared parts and run it.
    async fn execute_candidate(
        &self,
        parsed: ParsedFilters,
        extensions: Vec<ExtensionFilter>,
        author_names: Option<Vec<String>>,
        recipient_names: Option<Vec<String>>,
    ) -> Result<Vec<Uuid>> {
        let candidate = CandidateFilters {
            extensions,
            extension_logic: parsed.extension_logic,
            ..parsed.inner
        };
        let (sql, params) = build_candidate_sql(
            &candidate,
            author_names.as_deref(),
            recipient_names.as_deref(),
        );
        let mut query = sqlx::query_scalar::<_, Uuid>(&sql);
        for param in &params {
            query = match param {
                Param::Text(text) => query.bind(text),
                Param::Uuid(id) => query.bind(id),
                Param::Json(value) => query.bind(value),
            };
        }
        query.fetch_all(&self.pool).await.map_err(db_err)
    }
}

/// Map the pure filter-validation failure onto the port error taxonomy:
/// unknown keys are unsupported filters, while a requested-but-unregistered
/// extension id is an unknown extension (the Python raises
/// `UnknownFilterExtension` for the latter, a distinct type). Total over the
/// two `FilterValidation` variants: no catch-all arm exists to waive.
fn map_filter_error(err: crate::filters::FilterValidation) -> Error {
    match err {
        crate::filters::FilterValidation::UnknownKeys { .. } => {
            Error::UnsupportedFilter(err.to_string())
        }
        crate::filters::FilterValidation::UnknownExtension { .. } => {
            Error::UnknownFilterExtension(err.to_string())
        }
    }
}

impl PassageRepo for PgPassageRepo {
    type Tx = PgTx;
    async fn insert_many(
        &self,
        tx: &mut Self::Tx,
        document_id: Uuid,
        drafts: Vec<PassageDraft>,
    ) -> Result<Vec<Passage>> {
        let mut results = Vec::with_capacity(drafts.len());
        for draft in &drafts {
            // `uuid7()` in Python; `now_v7` is the same version/variant scheme.
            let pid = Uuid::now_v7();
            // `hashlib.sha256(draft.text.encode()).digest()`: SHA-256 over
            // the UTF-8 bytes, byte-identical.
            let mut hasher = Sha256::new();
            hasher.update(draft.text.as_bytes());
            let content_hash = hasher.finalize().to_vec();
            // Schema `Integer` columns bind as `i32` (the `int4` wire type,
            // exactly what asyncpg sends for small Python ints).
            // `RETURNING` instead of insert-then-reselect: the Python re-reads
            // each row for server defaults such as `created_at`, and one
            // statement is atomic where two are not (a concurrent delete
            // between them left a fetch with no mappable row).
            let row: PassageRow = sqlx::query_as(
                "INSERT INTO core.passages (id, document_id, position, char_start, \
                 char_end, locator, text, token_count, chunker, chunker_version, \
                 metadata, content_hash, node_id) \
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) \
                 RETURNING id, document_id, position, char_start, char_end, \
                 locator, text, token_count, chunker, chunker_version, metadata, node_id, \
                 content_hash, created_at",
            )
            .bind(pid)
            .bind(document_id)
            .bind(draft.position as i32)
            .bind(draft.char_start as i32)
            .bind(draft.char_end as i32)
            .bind(Value::Object(draft.locator.clone()))
            .bind(&draft.text)
            .bind(draft.token_count.map(|count| count as i32))
            .bind(&draft.chunker)
            .bind(&draft.chunker_version)
            .bind(Value::Object(draft.metadata.clone()))
            .bind(&content_hash)
            .bind(draft.node_id)
            .fetch_one(tx.exec())
            .await
            .map_err(db_err)?;
            results.push(passage_from_row(row));
        }
        Ok(results)
    }
    async fn get(&self, passage_id: Uuid) -> Result<Option<Passage>> {
        let row = sqlx::query_as::<_, PassageRow>(&passage_select("id = $1"))
            .bind(passage_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(row.map(passage_from_row))
    }

    async fn get_by_document(&self, document_id: Uuid) -> Result<Vec<Passage>> {
        let rows =
            sqlx::query_as::<_, PassageRow>(&passage_select("document_id = $1 ORDER BY position"))
                .bind(document_id)
                .fetch_all(&self.pool)
                .await
                .map_err(db_err)?;
        Ok(rows.into_iter().map(passage_from_row).collect())
    }

    async fn get_context(
        &self,
        passage_id: Uuid,
        before: i64,
        after: i64,
    ) -> Result<(Vec<Passage>, Passage, Vec<Passage>)> {
        // One statement instead of three sequential fetches: the target, the
        // before-window, and the after-window ride as marked branches of a
        // single `UNION ALL`, partitioned in Rust below. Besides one round
        // trip instead of three, a single fallible boundary means one fault
        // (not three) covers it — sequential same-table reads leave
        // middle-fetch error arms no deterministic fault can fire.
        // Row multisets are identical to the three-query form (same
        // predicates; the window branches read the target's coordinates
        // through PK subselects), and ordering is reconstructed the same way
        // (before newest-first, then flipped to document order).
        // A non-positive window is `LIMIT 0`: empty, exactly like the old
        // branch guards (negative limits never reach SQL either way).
        let rows: Vec<ContextRow> = sqlx::query_as(&format!(
            "(SELECT {PASSAGE_COLUMNS}, 0 AS which FROM core.passages WHERE id = $1) \
             UNION ALL \
             (SELECT {PASSAGE_COLUMNS}, 1 AS which FROM core.passages \
             WHERE document_id = (SELECT document_id FROM core.passages WHERE id = $1) \
             AND position < (SELECT position FROM core.passages WHERE id = $1) \
             ORDER BY position DESC LIMIT $2) \
             UNION ALL \
             (SELECT {PASSAGE_COLUMNS}, 2 AS which FROM core.passages \
             WHERE document_id = (SELECT document_id FROM core.passages WHERE id = $1) \
             AND position > (SELECT position FROM core.passages WHERE id = $1) \
             ORDER BY position LIMIT $3)"
        ))
        .bind(passage_id)
        .bind(before.max(0))
        .bind(after.max(0))
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        // An empty answer means the target is missing: the window branches
        // compare against the target's own subselects, so they can only
        // match when it exists. The Python raises
        // `ValueError(f"Passage not found: {passage_id}")`; the typed
        // not-found carries the same fact.
        let mut target: Option<Passage> = None;
        let mut before_list: Vec<Passage> = Vec::new();
        let mut after_list: Vec<Passage> = Vec::new();
        for row in rows {
            match row.which {
                0 => target = Some(passage_from_row(row.into_passage())),
                1 => before_list.push(passage_from_row(row.into_passage())),
                _ => after_list.push(passage_from_row(row.into_passage())),
            }
        }
        let Some(target) = target else {
            return Err(Error::NotFound {
                kind: "passage",
                id: passage_id.to_string(),
            });
        };
        // The window branch reads newest-first for the LIMIT, then flips to
        // document order.
        before_list.reverse();
        Ok((before_list, target, after_list))
    }
    async fn vector_search(
        &self,
        query_embedding: &[f64],
        model: &str,
        model_version: &str,
        candidate_ids: Option<&[Uuid]>,
        k: i64,
    ) -> Result<Vec<(Uuid, f64)>> {
        // The query embedding crosses as a `vector` literal in Python float
        // rendering (see `format_vector_py` and its bit-level proof).
        // digit-level proof). `candidate_ids` crosses as a `UUID[]` for
        // `= ANY(...)`, empty with `no_filter` true.
        let embedding_str = format_vector_py(query_embedding);
        let no_filter = candidate_ids.is_none();
        let candidates: Vec<Uuid> = candidate_ids.unwrap_or(&[]).to_vec();
        // Scores are `float8` on the wire (probed: `pg_typeof(1 - (embedding
        // <=> ...))` is `double precision`), so `f64` decodes bit-exactly to
        // the Python `float`. Ordering ties are the index's business: where
        // Postgres owns the order (HNSW approximate scan), scores-per-id are
        // the contract — the row order itself is not asserted anywhere.
        if let Some(ef_search) = self.ef_search {
            // Explicit transaction scope for `SET LOCAL`; rolls back
            // internally on any failure so the pooled connection is never
            // returned mid-transaction.
            let mut conn = self.pool.acquire().await.map_err(db_err)?;
            // Roll back before propagating any failure: dropping this
            // connection with an open transaction would poison the pool for
            // its next borrower (`25P02` far from the crime — caught by the
            // integration suite's aborted-backend guard). Both arms fire
            // across the happy and fault tests.
            match Self::vector_search_ef(
                &mut conn,
                ef_search,
                &embedding_str,
                model,
                model_version,
                no_filter,
                &candidates,
                k,
            )
            .await
            {
                Ok(rows) => Ok(rows),
                Err(e) => {
                    let _ = sqlx::query("ROLLBACK").execute(&mut *conn).await;
                    Err(e)
                }
            }
        } else {
            self.vector_search_plain(
                &embedding_str,
                model,
                model_version,
                no_filter,
                &candidates,
                k,
            )
            .await
        }
    }

    async fn keyword_search(
        &self,
        query: &str,
        lang: Option<&str>,
        candidate_ids: Option<&[Uuid]>,
        k: i64,
    ) -> Result<Vec<(Uuid, f64)>> {
        // `lang` is a Postgres regconfig. `Some` narrows to that config when
        // vouched for (else nothing: an unvalidated config must not reach
        // SQL, where it would be interpolated as a literal); `None` spans
        // every config present in the corpus.
        let configs: Vec<String> = match lang {
            Some(lang) if marginalia_chunk::langconfig::is_known_config(lang) => {
                vec![lang.to_string()]
            }
            Some(_) => Vec::new(),
            None => self.distinct_lang_configs().await?,
        };
        if configs.is_empty() {
            return Ok(Vec::new());
        }
        // Total over validated inputs: `configs` is non-empty (early return
        // above) and every entry vouched for (the `is_known_config` match
        // guard, or the `distinct_lang_configs` filter) — the builder's two
        // refusals are unreachable here, so `expect` (not a mappable error)
        // is the honest shape, with no dead arm to waive.
        let template =
            build_keyword_search_sql(&configs.iter().map(String::as_str).collect::<Vec<_>>())
                .expect("keyword configs validated before building");
        // The builder emits SQLAlchemy `text()`-style named binders, kept
        // verbatim for this pass, which binds them positionally: every
        // `:query` is `$1`, `:no_filter` is `$2`, `:candidate_ids` is `$3`,
        // `:k` is `$4`. Replacement order matters (`:candidate_ids` first so
        // the shorter names never match inside a longer one); `::regconfig`
        // casts contain no such binder. Covered by `keyword_binds_rewrite`
        // below.
        let sql = template
            .replace(":candidate_ids", "$3")
            .replace(":no_filter", "$2")
            .replace(":query", "$1")
            .replace(":k", "$4");
        let no_filter = candidate_ids.is_none();
        let candidates: Vec<Uuid> = candidate_ids.unwrap_or(&[]).to_vec();
        // `ts_rank_cd` returns `real` (probed `float4`): decode `f32`, then
        // widen — the exact widening asyncpg performs turning `float4` into
        // a Python `float`. Same tie-ordering contract as `vector_search`.
        let rows: Vec<(Uuid, f32)> = sqlx::query_as(&sql)
            .bind(query)
            .bind(no_filter)
            .bind(&candidates)
            .bind(k)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(rows
            .into_iter()
            .map(|(id, score)| (id, f64::from(score)))
            .collect())
    }

    async fn store_embeddings(
        &self,
        tx: &mut Self::Tx,
        passage_ids: &[Uuid],
        embeddings: &[Vec<f64>],
        model: &str,
        model_version: &str,
        dim: i64,
    ) -> Result<()> {
        // Plain inserts, zipped (truncating, like Python's non-strict zip):
        // re-embedding a passage is a delete-then-insert upstream, never an
        // upsert here. Each vector crosses as `::vector`-cast text via
        // `format_vector_py` — the same text the pgvector codec sends.
        for (passage_id, embedding) in passage_ids.iter().zip(embeddings.iter()) {
            sqlx::query(
                "INSERT INTO core.passage_embeddings (passage_id, model, model_version, \
                 dim, embedding) VALUES ($1, $2, $3, $4, CAST($5 AS vector))",
            )
            .bind(passage_id)
            .bind(model)
            .bind(model_version)
            .bind(dim as i32)
            .bind(format_vector_py(embedding))
            .execute(tx.exec())
            .await
            .map_err(db_err)?;
        }
        Ok(())
    }

    async fn index_fts(
        &self,
        tx: &mut Self::Tx,
        passage_ids: &[Uuid],
        texts: &[String],
        lang: &str,
    ) -> Result<()> {
        // The upsert refreshes `lang_config` as well as `ts`: updating only
        // the vector would leave the column describing a stemming that no
        // longer applies, and `keyword_search` routes on that column.
        if !marginalia_chunk::langconfig::is_known_config(lang) {
            // Byte-identical to the Python `ValueError(f"...{lang!r}")`:
            // `{!r}` on a `str` is single quotes, which `{:?}` would not be.
            return Err(Error::Validation(format!(
                "Unknown text-search config: '{lang}'"
            )));
        }
        for (passage_id, text) in passage_ids.iter().zip(texts.iter()) {
            // `CAST(... AS regconfig)`, not `::regconfig`: `::` reads as part
            // of a bind-parameter name and would leave it unsubstituted.
            sqlx::query(
                "INSERT INTO core.passage_fts (passage_id, lang_config, ts) \
                 VALUES ($1, CAST($2 AS regconfig), \
                 to_tsvector(CAST($2 AS regconfig), $3)) \
                 ON CONFLICT (passage_id) DO UPDATE \
                 SET lang_config = EXCLUDED.lang_config, ts = EXCLUDED.ts",
            )
            .bind(passage_id)
            .bind(lang)
            .bind(text)
            .execute(tx.exec())
            .await
            .map_err(db_err)?;
        }
        Ok(())
    }

    async fn get_embedding(
        &self,
        passage_id: Uuid,
        model: &str,
        model_version: &str,
    ) -> Result<Option<Vec<f64>>> {
        let text: Option<String> = sqlx::query_scalar(
            "SELECT embedding::text FROM core.passage_embeddings \
             WHERE passage_id = $1 AND model = $2 AND model_version = $3",
        )
        .bind(passage_id)
        .bind(model)
        .bind(model_version)
        .fetch_optional(&self.pool)
        .await
        .map_err(db_err)?;
        text.map(|text| parse_vector_text(&text)).transpose()
    }

    async fn filter_candidate_ids<F: FilterExtension>(
        &self,
        filters: &Map<String, Value>,
        filter_extensions: Option<&HashMap<String, F>>,
    ) -> Result<Vec<Uuid>> {
        // The only clause representation this implementation can execute is
        // the crate's own `ExtensionClause` (see the `FilterExtension for
        // ExtensionClause` docs in `repos.rs` and
        // `filter_candidate_ids_with_clauses` below). `F::Clause` is opaque
        // here, and rendering an arbitrary clause type would need either
        // specialization (unstable) or an extra bound (which narrows the
        // port contract) — method resolution in a generic body cannot
        // dispatch on `F::Clause` even when it happens to be
        // `ExtensionClause`, so there is no sound way to inline unknown
        // clauses. Extension-free requests run the full pipeline; requested
        // extensions fail loud naming the concrete clause type, instead of
        // running a query the caller believes is filtered but is not.
        // Registries of prebuilt clauses use the inherent
        // `filter_candidate_ids_with_clauses`.
        let parsed = parse_candidate_filters(filters)?;
        let available: Vec<&str> = filter_extensions
            .map(|registry| {
                let mut keys: Vec<&str> = registry.keys().map(String::as_str).collect();
                keys.sort_unstable();
                keys
            })
            .unwrap_or_default();
        let filter_keys: Vec<&str> = parsed.filter_keys.iter().map(String::as_str).collect();
        let extension_ids = parsed.extension_ids();
        // Fail loud on unknown keys/extension ids before touching the
        // database, exactly like the Python `validate_filters` — including
        // when the request will go on to refuse below.
        crate::filters::validate_filters(&filter_keys, &extension_ids, &available)
            .map_err(map_filter_error)?;
        if !parsed.extensions.is_empty() {
            return Err(Error::Plugin(format!(
                "filter extension clause type {} is not executable through \
                 the generic port method: pass a prebuilt-clause registry to \
                 filter_candidate_ids_with_clauses",
                std::any::type_name::<F::Clause>(),
            )));
        }
        // Entity-name resolution needs I/O and happens here; the resolved
        // names ride into the pure builder as `author_names`/`recipient_names`.
        let author_names = self.resolve_entity_names(parsed.author_entity_id).await?;
        let recipient_names = self
            .resolve_entity_names(parsed.recipient_entity_id)
            .await?;
        self.execute_candidate(parsed, Vec::new(), author_names, recipient_names)
            .await
    }

    async fn covering_span(
        &self,
        document_id: Uuid,
        char_start: i64,
        char_end: i64,
    ) -> Result<Vec<Passage>> {
        // A verified quotation is a span of the *document*, not of a
        // passage — quotes straddle chunk boundaries routinely — so every
        // passage the span touches comes back. Served by
        // `passages_doc_span_idx`.
        let rows = sqlx::query_as::<_, PassageRow>(&passage_select(
            "document_id = $1 AND char_start < $2 AND char_end > $3 \
             ORDER BY char_start",
        ))
        .bind(document_id)
        .bind(char_end as i32)
        .bind(char_start as i32)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(rows.into_iter().map(passage_from_row).collect())
    }

    async fn count(&self) -> Result<i64> {
        sqlx::query_scalar("SELECT count(*) FROM core.passages")
            .fetch_one(&self.pool)
            .await
            .map_err(db_err)
    }
}

impl PgPassageRepo {
    /// The regconfigs actually present in `passage_fts`, briefly cached
    /// upstream in Python (60 s TTL) and re-queried here per call.
    ///
    /// The TTL is a pure performance cache: dropping it changes no
    /// observable result (ingesting a new language is visible immediately
    /// rather than within a minute), and one `DISTINCT` scan per unfiltered
    /// search is cheap at two-or-three configs in practice. Unknown configs
    /// are skipped, never interpolated — interpolating one into SQL would be
    /// worse than losing recall for that language.
    async fn distinct_lang_configs(&self) -> Result<Vec<String>> {
        let rows: Vec<(String,)> =
            sqlx::query_as("SELECT DISTINCT lang_config::text AS cfg FROM core.passage_fts")
                .fetch_all(&self.pool)
                .await
                .map_err(db_err)?;
        let mut configs: Vec<String> = rows
            .into_iter()
            .map(|(cfg,)| cfg)
            .filter(|cfg| marginalia_chunk::langconfig::is_known_config(cfg))
            .collect();
        configs.sort();
        Ok(configs)
    }
}

/// One `core.passages` row, decoded by column name.
///
/// Schema `Integer` columns decode as `i32` (the `int4` wire type) and widen
/// to the domain's `i64`; `locator`/`metadata` decode optional and default
/// to `{}` (the Python `row.locator or {}`).
#[derive(Debug, Clone, FromRow)]
pub(crate) struct PassageRow {
    pub id: Uuid,
    pub document_id: Uuid,
    pub position: i32,
    pub char_start: Option<i32>,
    pub char_end: Option<i32>,
    pub locator: Option<Value>,
    pub text: String,
    pub token_count: Option<i32>,
    pub chunker: String,
    pub chunker_version: String,
    pub metadata: Option<Value>,
    pub node_id: Option<Uuid>,
    pub content_hash: Vec<u8>,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

/// Map a row to the domain type.
pub(crate) fn passage_from_row(row: PassageRow) -> Passage {
    Passage {
        id: row.id,
        document_id: row.document_id,
        position: i64::from(row.position),
        char_start: row.char_start.map(i64::from),
        char_end: row.char_end.map(i64::from),
        locator: json_object_or_empty(row.locator),
        text: row.text,
        token_count: row.token_count.map(i64::from),
        chunker: row.chunker,
        chunker_version: row.chunker_version,
        metadata: json_object_or_empty(row.metadata),
        node_id: row.node_id,
        content_hash: row.content_hash,
        created_at: row.created_at,
    }
}

/// One `get_context` `UNION ALL` branch row: a passage plus the branch
/// marker (`0` target, `1` before-window, `2` after-window) the method
/// partitions on.
#[derive(Debug, FromRow)]
struct ContextRow {
    #[sqlx(flatten)]
    passage: PassageRow,
    which: i32,
}

impl ContextRow {
    fn into_passage(self) -> PassageRow {
        self.passage
    }
}

fn json_object_or_empty(value: Option<Value>) -> Map<String, Value> {
    match value {
        Some(Value::Object(map)) => map,
        _ => Map::new(),
    }
}

/// All passage columns, unaliased.
const PASSAGE_COLUMNS: &str = "id, document_id, position, char_start, char_end, \
     locator, text, token_count, chunker, chunker_version, metadata, node_id, \
     content_hash, created_at";

/// `SELECT <all passage columns> FROM core.passages WHERE <predicate>`.
fn passage_select(predicate: &str) -> String {
    format!("SELECT {PASSAGE_COLUMNS} FROM core.passages WHERE {predicate}")
}

/// The same column list qualified with an alias, for join queries where
/// bare names would be ambiguous.
fn passage_select_aliased(alias: &str, rest: &str) -> String {
    let columns: Vec<String> = PASSAGE_COLUMNS
        .split(", ")
        .map(|col| format!("{alias}.{col}"))
        .collect();
    format!(
        "SELECT {} FROM core.passages {alias} {rest}",
        columns.join(", ")
    )
}

/// Parse one stored `embedding::text` (`"[0.1,0.3,...]"`) back to floats.
///
/// pgvector stores one `float32` per dimension and its text output is the
/// shortest round-trip of each stored value, so parsing as `f32` recovers
/// the stored bits exactly and the `as f64` widening is exact too. That is
/// precisely the Python path, probed end to end: pgvector-SQLAlchemy
/// decodes `Vector` columns to a `float32` ndarray, and `get_embedding`
/// does `list(...)` over it — each element widened from `float32` to
/// Python `float`. Parsing the same text as `f64` directly would instead
/// round to the nearest `float64` decimal and differ in the low bits
/// (e.g. stored `0.1f32` prints `"0.1"`, which parses as `f64` to
/// `0.1000000000000000055` but widens from `f32` to
/// `0.10000000149011612`).
pub(crate) fn parse_vector_text(text: &str) -> Result<Vec<f64>> {
    // No empty-input shortcut: the column is `vector(1024) NOT NULL`, so a
    // stored vector always has elements — `"[]"` fails loudly below like
    // any other malformed text, and there is no arm for pg_repos to miss.
    let inner = text
        .strip_prefix('[')
        .and_then(|s| s.strip_suffix(']'))
        .ok_or_else(|| Error::Storage(format!("cannot parse vector text: {text:?}")))?;
    inner
        .split(',')
        .map(|part| {
            part.parse::<f32>()
                .map(f64::from)
                .map_err(|_| Error::Storage(format!("cannot parse vector element: {part:?}")))
        })
        .collect()
}

/// `author_entity_id` / `recipient_entity_id` stay as ids here: entity-name
/// resolution needs I/O and runs in the repository method.
struct ParsedFilters {
    /// Pure builder input (extensions filled in by the caller afterwards).
    inner: CandidateFilters,
    /// Raw filter keys, for [`validate_filters`](crate::filters::validate_filters).
    filter_keys: Vec<String>,
    author_entity_id: Option<Uuid>,
    recipient_entity_id: Option<Uuid>,
    /// Requested extensions in map order with their raw values.
    extensions: Vec<(String, Value)>,
    extension_logic: ExtensionLogic,
}

impl ParsedFilters {
    fn extension_ids(&self) -> Vec<&str> {
        self.extensions.iter().map(|(id, _)| id.as_str()).collect()
    }
}

/// Parse the raw filter map into [`ParsedFilters`], mirroring the Python
/// `SearchFilters` field types. Wrong JSON shapes fail loud with `Validation`
/// rather than being ignored or coerced.
fn parse_candidate_filters(filters: &Map<String, Value>) -> Result<ParsedFilters> {
    fn string_array(value: &Value, key: &str) -> Result<Vec<String>> {
        match value {
            Value::Array(items) => items
                .iter()
                .map(|item| {
                    item.as_str().map(str::to_string).ok_or_else(|| {
                        Error::Validation(format!("filter {key:?} must be an array of strings"))
                    })
                })
                .collect(),
            _ => Err(Error::Validation(format!(
                "filter {key:?} must be an array of strings"
            ))),
        }
    }

    fn opt_string(value: Option<&Value>, key: &str) -> Result<Option<String>> {
        match value {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(text)) => Ok(Some(text.clone())),
            Some(_) => Err(Error::Validation(format!(
                "filter {key:?} must be a string"
            ))),
        }
    }

    /// An optional RFC3339 timestamp filter, normalized back to RFC3339
    /// so the builder's inline `::timestamptz` cast always parses.
    /// Python receives `datetime` objects here (a raw string would mistype
    /// as `VARCHAR` there too); the JSON boundary carries strings, so they
    /// are validated rather than trusted.
    fn opt_rfc3339(value: Option<&Value>, key: &str) -> Result<Option<String>> {
        match opt_string(value, key)? {
            None => Ok(None),
            Some(text) => chrono::DateTime::parse_from_rfc3339(&text)
                .map(|fixed| Some(fixed.to_utc().to_rfc3339()))
                .map_err(|_| {
                    Error::Validation(format!("filter {key:?} must be an RFC3339 timestamp"))
                }),
        }
    }

    fn opt_uuid(value: Option<&Value>, key: &str) -> Result<Option<Uuid>> {
        match opt_string(value, key)? {
            None => Ok(None),
            Some(text) => text
                .parse::<Uuid>()
                .map(Some)
                .map_err(|_| Error::Validation(format!("filter {key:?} must be a UUID string"))),
        }
    }

    let filter_keys: Vec<String> = filters.keys().cloned().collect();

    let document_types = match filters.get("document_types") {
        None | Some(Value::Null) => Vec::new(),
        Some(value) => string_array(value, "document_types")?,
    };
    let mentions_entity_ids = match filters.get("mentions_entity_ids") {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .ok_or_else(|| {
                        Error::Validation(
                            "filter \"mentions_entity_ids\" must be UUID strings".to_string(),
                        )
                    })
                    .and_then(|text| {
                        text.parse::<Uuid>().map_err(|_| {
                            Error::Validation(
                                "filter \"mentions_entity_ids\" must be UUID strings".to_string(),
                            )
                        })
                    })
            })
            .collect::<Result<Vec<Uuid>>>()?,
        Some(_) => {
            return Err(Error::Validation(
                "filter \"mentions_entity_ids\" must be an array".to_string(),
            ));
        }
    };
    let (extensions, extension_logic) = match filters.get("extensions") {
        None | Some(Value::Null) => (Vec::new(), ExtensionLogic::default()),
        Some(Value::Object(map)) => {
            let logic = match filters.get("extension_logic") {
                Some(Value::String(text)) => ExtensionLogic::parse(text),
                _ => ExtensionLogic::default(),
            };
            (
                map.iter()
                    .map(|(id, value)| (id.clone(), value.clone()))
                    .collect(),
                logic,
            )
        }
        Some(_) => {
            return Err(Error::Validation(
                "filter \"extensions\" must be an object".to_string(),
            ));
        }
    };

    Ok(ParsedFilters {
        inner: CandidateFilters {
            document_types,
            date_range_start: opt_rfc3339(filters.get("date_range_start"), "date_range_start")?,
            date_range_end: opt_rfc3339(filters.get("date_range_end"), "date_range_end")?,
            mentions_entity_ids,
            metadata: filters.get("metadata").cloned(),
            language: opt_string(filters.get("language"), "language")?,
            ..Default::default()
        },
        filter_keys,
        author_entity_id: opt_uuid(filters.get("author_entity_id"), "author_entity_id")?,
        recipient_entity_id: opt_uuid(filters.get("recipient_entity_id"), "recipient_entity_id")?,
        extensions,
        extension_logic,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::filters::ExtensionClause;

    // Mirrors `tests/unit/adapters/test_repository_surface.py` for
    // `PGPassageRepo`: every expected method must exist with a compatible
    // shape. The generic `filter_candidate_ids` is pinned with the SQL
    // clause binding this pass establishes.
    #[test]
    fn repository_exposes_expected_methods() {
        let _ = <PgPassageRepo as PassageRepo>::insert_many;
        let _ = <PgPassageRepo as PassageRepo>::get;
        let _ = <PgPassageRepo as PassageRepo>::get_by_document;
        let _ = <PgPassageRepo as PassageRepo>::get_context;
        let _ = <PgPassageRepo as PassageRepo>::vector_search;
        let _ = <PgPassageRepo as PassageRepo>::keyword_search;
        let _ = <PgPassageRepo as PassageRepo>::store_embeddings;
        let _ = <PgPassageRepo as PassageRepo>::index_fts;
        let _ = <PgPassageRepo as PassageRepo>::get_embedding;
        let _ = <PgPassageRepo as PassageRepo>::filter_candidate_ids::<ExtensionClause>;
        let _ = PgPassageRepo::filter_candidate_ids_with_clauses;
        let _ = <PgPassageRepo as PassageRepo>::covering_span;
        let _ = PgPassageRepo::get_many;
        let _ = PgPassageRepo::get_by_node;
        let _ = PgPassageRepo::relabel_version;
        let _ = PgPassageRepo::set_locators;
        let _ = PgPassageRepo::set_node_ids;
        let _ = PgPassageRepo::new;
        let _ = PgPassageRepo::with_ef_search;
    }

    #[test]
    fn row_mapping_defaults_locator_and_metadata_to_empty() {
        // The Python `row.locator or {}` / `row.metadata or {}`.
        let row = PassageRow {
            id: Uuid::now_v7(),
            document_id: Uuid::now_v7(),
            position: 3,
            char_start: Some(0),
            char_end: Some(9),
            locator: None,
            text: "some text".to_string(),
            token_count: None,
            chunker: "structural".to_string(),
            chunker_version: "4.0".to_string(),
            metadata: None,
            node_id: None,
            content_hash: vec![1, 2, 3],
            created_at: chrono::Utc::now(),
        };
        let passage = passage_from_row(row);
        assert_eq!(passage.position, 3);
        assert_eq!(passage.char_start, Some(0));
        assert!(passage.locator.is_empty());
        assert!(passage.metadata.is_empty());
        assert_eq!(passage.content_hash, vec![1, 2, 3]);
    }

    #[test]
    fn keyword_binds_rewrite_positionally() {
        // The `filters` builder emits SQLAlchemy named binders; this pass
        // binds them as `$1..$4`. Two configs prove every branch rewrites.
        let template = build_keyword_search_sql(&["english", "german"]).expect("known");
        let sql = template
            .replace(":candidate_ids", "$3")
            .replace(":no_filter", "$2")
            .replace(":query", "$1")
            .replace(":k", "$4");
        assert_eq!(sql.matches("$1").count(), 2, "{sql}");
        assert_eq!(sql.matches("$2").count(), 2, "{sql}");
        assert_eq!(sql.matches("$3").count(), 2, "{sql}");
        assert!(sql.contains("LIMIT $4"), "{sql}");
        // No SQLAlchemy named binder may survive (`::regconfig` casts are
        // expected and contain no binder names).
        for binder in [":query", ":no_filter", ":candidate_ids"] {
            assert!(!sql.contains(binder), "binder {binder} survived: {sql}");
        }
        assert!(sql.contains("plainto_tsquery('english', $1)"), "{sql}");
        assert!(sql.contains("plainto_tsquery('german', $1)"), "{sql}");
    }

    #[test]
    fn unknown_fts_lang_message_is_byte_identical() {
        // The Python `ValueError(f"Unknown text-search config: {lang!r}")`
        // uses `{!r}` (single quotes); `{:?}` would emit double quotes.
        let msg = format!("Unknown text-search config: '{}'", "xx");
        assert_eq!(msg, "Unknown text-search config: 'xx'");
    }

    #[test]
    fn stored_vector_parses_through_float32() {
        // pgvector prints the shortest round-trip of each stored `float32`:
        // parsing as `f32` recovers the stored bits, and widening is exact.
        // `0.1f32 as f64` is `0.10000000149011612` — parsing as `f64`
        // directly would give `0.1` instead and break bit parity.
        let parsed = parse_vector_text("[0.1,0.3,1.5]").expect("parses");
        assert_eq!(parsed.len(), 3);
        assert_eq!(parsed[0].to_bits(), f64::from(0.1f32).to_bits());
        assert_eq!(parsed[0].to_bits(), 0.10000000149011612f64.to_bits());
        assert!(parse_vector_text("[]").is_err());
        assert!(parse_vector_text("nope").is_err());
    }
    #[test]
    fn corrupt_vector_elements_refuse_with_the_element() {
        // The bracket shape passed but an element is not a float: the error
        // names it rather than failing the whole text opaquely.
        let err = parse_vector_text("[0.1,xyz]").expect_err("must refuse");
        assert!(err.to_string().contains("\"xyz\""), "{err:?}");
    }

    #[test]
    fn date_bounds_carry_timestamptz_casts() {
        // The cast lives in the pure builder (see `filters::build_candidate_sql`),
        // so by the time the SQL reaches execution there is nothing to rewrite.
        let filters = CandidateFilters {
            date_range_start: Some("1800-01-01T00:00:00+00:00".to_string()),
            date_range_end: Some("1850-01-01T00:00:00+00:00".to_string()),
            ..Default::default()
        };
        let (sql, _) = build_candidate_sql(&filters, None, None);
        assert!(
            sql.contains("created_date_start >= $1::timestamptz"),
            "{sql}"
        );
        assert!(sql.contains("created_date_end <= $2::timestamptz"), "{sql}");
    }

    #[test]
    fn candidate_filter_parsing_rejects_wrong_shapes() {
        let mut filters = Map::new();
        filters.insert(
            "mentions_entity_ids".to_string(),
            Value::Array(vec![Value::String("not-a-uuid".to_string())]),
        );
        assert!(parse_candidate_filters(&filters).is_err());
        let mut filters = Map::new();
        filters.insert("extensions".to_string(), Value::String("nope".to_string()));
        assert!(parse_candidate_filters(&filters).is_err());
    }

    #[test]
    fn filter_error_mapping_is_total() {
        // Variant identity via discriminants: no `match`/`matches!` false-arm
        // for the same fact (see the `argument` module note). Total over two
        // variants — no catch-all arm exists.
        use crate::filters::FilterValidation;
        use std::mem::discriminant;
        let err = map_filter_error(FilterValidation::UnknownExtension {
            id: "x".to_string(),
            available: vec!["y".to_string()],
        });
        assert_eq!(
            discriminant(&err),
            discriminant(&Error::UnknownFilterExtension(String::new()))
        );
        let err = map_filter_error(FilterValidation::UnknownKeys {
            unknown: vec!["nope".to_string()],
            supported: Vec::new(),
        });
        assert_eq!(
            discriminant(&err),
            discriminant(&Error::UnsupportedFilter(String::new()))
        );
    }

    #[test]
    fn candidate_filter_parsing_rejects_malformed_values() {
        // Non-string items in string arrays, and non-string scalars.
        for key in ["document_types", "language", "author_entity_id"] {
            let mut filters = Map::new();
            filters.insert(key.to_string(), Value::Number(42.into()));
            assert!(parse_candidate_filters(&filters).is_err(), "{key}");
        }
        let mut filters = Map::new();
        filters.insert(
            "document_types".to_string(),
            Value::Array(vec![Value::Number(1.into())]),
        );
        assert!(parse_candidate_filters(&filters).is_err());
        let mut filters = Map::new();
        filters.insert(
            "mentions_entity_ids".to_string(),
            Value::Array(vec![Value::Number(1.into())]),
        );
        assert!(parse_candidate_filters(&filters).is_err());
        let mut filters = Map::new();
        filters.insert(
            "date_range_start".to_string(),
            Value::String("not-a-date".to_string()),
        );
        assert!(parse_candidate_filters(&filters).is_err());
        let mut filters = Map::new();
        filters.insert("date_range_start".to_string(), Value::Number(42.into()));
        assert!(parse_candidate_filters(&filters).is_err());
        // Every keyed extractor needs its own fault: same-shaped arms on
        // different keys are distinct regions.
        for (key, value) in [
            ("mentions_entity_ids", Value::Number(42.into())),
            ("date_range_end", Value::String("not-a-date".to_string())),
            ("date_range_end", Value::Number(42.into())),
            ("author_entity_id", Value::String("not-a-uuid".to_string())),
            (
                "recipient_entity_id",
                Value::String("not-a-uuid".to_string()),
            ),
        ] {
            let mut filters = Map::new();
            filters.insert(key.to_string(), value);
            assert!(parse_candidate_filters(&filters).is_err(), "{key}");
        }
    }

    #[test]
    fn candidate_filter_parsing_keeps_extension_order() {
        let mut filters = Map::new();
        let mut extensions = Map::new();
        extensions.insert("b_ext".to_string(), Value::Null);
        extensions.insert("a_ext".to_string(), Value::Null);
        filters.insert("extensions".to_string(), Value::Object(extensions));
        filters.insert(
            "extension_logic".to_string(),
            Value::String("or".to_string()),
        );
        let parsed = parse_candidate_filters(&filters).expect("parses");
        assert_eq!(parsed.extension_ids(), vec!["b_ext", "a_ext"]);
        assert_eq!(parsed.extension_logic, ExtensionLogic::Or);
    }
}
