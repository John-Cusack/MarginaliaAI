//! `DocumentRepo` over Postgres (`sqlx`).
//!
//! Python source:
//! `adapters/storage/postgres/repositories/documents.py`. Table and column
//! names come from `adapters/storage/postgres/schema.py` (`core.documents`:
//! `id`, `title`, `document_type`, `language`, `source`, `content_hash`,
//! `parser`, `parser_version`, `ingested_at`, `created_date_start`,
//! `created_date_end`, `created_precision`, `edition_id`, `metadata`;
//! `edition_id` arrived with migration 020).

use chrono::{DateTime, Utc};
use marginalia_types::documents::{Document, DocumentDraft, DocumentFilter};
use marginalia_types::errors::{Error, Result};
use marginalia_types::ports::DocumentRepo;
use serde_json::{Map, Value};
use sqlx::{FromRow, PgPool};
use uuid::Uuid;

use super::{db_err, PgTx};

/// The repository. Holds a [`PgPool`]; transactional writes take a
/// [`PgTx`] opened via [`PgTx::begin`].
pub struct PgDocumentRepo {
    pool: PgPool,
}

impl PgDocumentRepo {
    /// Serve the repository from an existing pool.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Documents whose `metadata` holds `key == value`.
    ///
    /// Packs identify their own material by a key they wrote at ingest (the
    /// Logos pack uses `resource_id`). Extra method mirroring
    /// `PGDocumentRepo.find_by_metadata`; not on the port trait.
    pub async fn find_by_metadata(&self, key: &str, value: &str) -> Result<Vec<Document>> {
        // `metadata` is a `json` column, so the containment read casts to
        // `jsonb` at query time (see the schema note about the missing GIN
        // index): `metadata->>key` extracts as text for the equality.
        let rows = sqlx::query_as::<_, DocumentRow>(
            "SELECT id, title, document_type, language, source, content_hash, \
             parser, parser_version, ingested_at, created_date_start, \
             created_date_end, created_precision, edition_id, metadata \
             FROM core.documents WHERE CAST(metadata AS JSONB) ->> $1 = $2",
        )
        .bind(key)
        .bind(value)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(rows.into_iter().map(document_from_row).collect())
    }

    /// The read-modify-write itself, separated so the transaction above can
    /// roll back on any failure before propagating it.
    async fn update_merged(
        tx: &mut PgTx,
        doc_id: Uuid,
        patch: Map<String, Value>,
    ) -> Result<Document> {
        let existing: Option<Value> =
            sqlx::query_scalar("SELECT metadata FROM core.documents WHERE id = $1")
                .bind(doc_id)
                .fetch_optional(tx.exec())
                .await
                .map_err(db_err)?;
        // No early `NotFound` on a missing row: it merges over `{}`, and the
        // `UPDATE ... RETURNING` below then matches nothing and reports the
        // same `NotFound` — one `None` arm instead of two. A scalar
        // `metadata` (schema drift or a manual write; the column is NOT
        // NULL-able, so `NULL` cannot occur) merges as `{}` rather than
        // crashing: the Python `row.metadata or {}` keeps a truthy scalar
        // and then fails unpacking it, which no writer can produce either.
        let mut merged: Map<String, Value> = match existing {
            Some(Value::Object(map)) => map,
            _ => Map::new(),
        };
        merged.extend(patch);
        // `RETURNING` instead of update-then-reselect: one atomic statement,
        // so no re-read can observe a concurrent delete.
        let row: Option<DocumentRow> = sqlx::query_as(
            "UPDATE core.documents SET metadata = $1 WHERE id = $2 \
             RETURNING id, title, document_type, language, source, content_hash, \
             parser, parser_version, ingested_at, created_date_start, \
             created_date_end, created_precision, edition_id, metadata",
        )
        .bind(Value::Object(merged))
        .bind(doc_id)
        .fetch_optional(tx.exec())
        .await
        .map_err(db_err)?;
        row.map(document_from_row).ok_or(Error::NotFound {
            kind: "document",
            id: doc_id.to_string(),
        })
    }
}

impl DocumentRepo for PgDocumentRepo {
    type Tx = PgTx;

    async fn insert(&self, tx: &mut Self::Tx, draft: DocumentDraft) -> Result<Document> {
        let doc_id = Uuid::now_v7();
        // `uuid7()` in Python; `now_v7` is the same version/variant scheme.
        let metadata = Value::Object(draft.metadata.clone());
        // `RETURNING *` instead of insert-then-reselect: the Python re-reads
        // the row for server defaults such as `ingested_at`, and a reselect
        // could in theory observe a concurrent delete under READ COMMITTED
        // where `RETURNING` cannot — one statement is atomic, so the row is
        // present by construction and there is no mappable `None` arm.
        let row = sqlx::query_as::<_, DocumentRow>(
            "INSERT INTO core.documents (id, title, document_type, language, \
             source, content_hash, parser, parser_version, created_date_start, \
             created_date_end, created_precision, edition_id, metadata) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) \
             RETURNING id, title, document_type, language, source, content_hash, \
             parser, parser_version, ingested_at, created_date_start, \
             created_date_end, created_precision, edition_id, metadata",
        )
        .bind(doc_id)
        .bind(draft.title.as_deref())
        .bind(&draft.document_type)
        .bind(draft.language.as_deref())
        .bind(&draft.source)
        .bind(&draft.content_hash)
        .bind(&draft.parser)
        .bind(&draft.parser_version)
        .bind(draft.created_date_start)
        .bind(draft.created_date_end)
        .bind(draft.created_precision.as_deref())
        .bind(draft.edition_id)
        .bind(&metadata)
        .fetch_one(tx.exec())
        .await
        .map_err(db_err)?;
        Ok(document_from_row(row))
    }

    async fn get(&self, doc_id: Uuid) -> Result<Option<Document>> {
        let row = sqlx::query_as::<_, DocumentRow>(&document_select("id = $1"))
            .bind(doc_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(row.map(document_from_row))
    }

    async fn get_many(&self, doc_ids: &[Uuid]) -> Result<Vec<Document>> {
        // The Python `get_many` returns rows in SELECT order with missing ids
        // omitted. This implementation issues the same statement, then orders
        // in Rust to the input order: deterministic for callers zipping
        // against the input, and identical as a set.
        if doc_ids.is_empty() {
            return Ok(Vec::new());
        }
        let rows = sqlx::query_as::<_, DocumentRow>(&document_select("id = ANY($1)"))
            .bind(doc_ids)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
        let mut by_id = std::collections::HashMap::with_capacity(rows.len());
        for row in rows {
            by_id.insert(row.id, document_from_row(row));
        }
        Ok(doc_ids.iter().filter_map(|id| by_id.remove(id)).collect())
    }

    async fn find_by_hash(&self, content_hash: &[u8], source: &str) -> Result<Option<Document>> {
        let row =
            sqlx::query_as::<_, DocumentRow>(&document_select("content_hash = $1 AND source = $2"))
                .bind(content_hash)
                .bind(source)
                .fetch_optional(&self.pool)
                .await
                .map_err(db_err)?;
        Ok(row.map(document_from_row))
    }

    async fn find_by_edition_id(
        &self,
        tx: &mut Self::Tx,
        edition_id: Uuid,
    ) -> Result<Option<Document>> {
        // Inside the caller's transaction by contract: the caller holds the
        // edition row locked, so the pool must not serve this read.
        // `LIMIT 1` mirrors the Python; `edition_id` is unique in practice
        // but the contract promises at most one row either way.
        let row = sqlx::query_as::<_, DocumentRow>(&document_select("edition_id = $1 LIMIT 1"))
            .bind(edition_id)
            .fetch_optional(tx.exec())
            .await
            .map_err(db_err)?;
        Ok(row.map(document_from_row))
    }
    async fn update_metadata(&self, doc_id: Uuid, patch: Map<String, Value>) -> Result<Document> {
        // Read-modify-write inside one transaction, exactly like the Python
        // `engine.begin()` block: the merge is `{**existing, **patch}`.
        let mut tx = PgTx::begin(&self.pool).await?;
        // Roll the transaction back before propagating any failure: returning
        // an aborted handle would poison the pooled connection for its next
        // borrower (the Python context manager this mirrors rolls back on
        // exception). Both arms fire across the happy and fault tests, so no
        // arm is dead.
        let result = Self::update_merged(&mut tx, doc_id, patch).await;
        match result {
            Ok(doc) => {
                // Tail position, not `?`: same mapping, no caller-side error
                // arm (a commit that can only fail on a dead connection has
                // no deterministic fault — the mapping is covered centrally).
                tx.commit().await.map(|_| doc)
            }
            Err(e) => {
                let _ = tx.rollback().await;
                Err(e)
            }
        }
    }

    async fn iter_by_filter(&self, filter: &DocumentFilter) -> Result<Vec<Document>> {
        // The port collects the Python async generator into a `Vec`
        // (see the `ports.rs` mapping note).
        let (sql, params) = filtered_select(filter, false);
        let rows = bind_doc_rows(sqlx::query_as::<_, DocumentRow>(&sql), &params)
            .fetch_all(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(rows.into_iter().map(document_from_row).collect())
    }

    async fn count(&self, filter: Option<&DocumentFilter>) -> Result<i64> {
        let (sql, params) = match filter {
            Some(filter) => filtered_select(filter, true),
            None => (
                "SELECT count(*) FROM core.documents".to_string(),
                Vec::new(),
            ),
        };
        bind_doc_count(sqlx::query_scalar::<_, i64>(&sql), &params)
            .fetch_one(&self.pool)
            .await
            .map_err(db_err)
    }

    async fn delete(&self, doc_id: Uuid) -> Result<()> {
        sqlx::query("DELETE FROM core.documents WHERE id = $1")
            .bind(doc_id)
            .execute(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(())
    }
}

/// One `core.documents` row, decoded by column name.
#[derive(Debug, Clone, FromRow)]
pub(crate) struct DocumentRow {
    pub id: Uuid,
    pub title: Option<String>,
    pub document_type: String,
    pub language: Option<String>,
    pub source: String,
    pub content_hash: Vec<u8>,
    pub parser: String,
    pub parser_version: String,
    pub ingested_at: DateTime<Utc>,
    pub created_date_start: Option<DateTime<Utc>>,
    pub created_date_end: Option<DateTime<Utc>>,
    pub created_precision: Option<String>,
    pub edition_id: Option<Uuid>,
    pub metadata: Option<Value>,
}

/// Map a row to the domain type: `metadata NULL` reads as `{}` (the Python
/// `row.metadata or {}`), and the hash crosses as raw bytes.
pub(crate) fn document_from_row(row: DocumentRow) -> Document {
    Document {
        id: row.id,
        title: row.title,
        document_type: row.document_type,
        language: row.language,
        source: row.source,
        content_hash: row.content_hash,
        parser: row.parser,
        parser_version: row.parser_version,
        ingested_at: row.ingested_at,
        created_date_start: row.created_date_start,
        created_date_end: row.created_date_end,
        created_precision: row.created_precision,
        edition_id: row.edition_id,
        metadata: match row.metadata {
            Some(Value::Object(map)) => map,
            _ => Map::new(),
        },
    }
}

/// `SELECT <all document columns> FROM core.documents WHERE <predicate>`.
fn document_select(predicate: &str) -> String {
    format!(
        "SELECT id, title, document_type, language, source, content_hash, \
         parser, parser_version, ingested_at, created_date_start, \
         created_date_end, created_precision, edition_id, metadata \
         FROM core.documents WHERE {predicate}"
    )
}

/// One ordered bind value for [`filtered_select`]: dates bind as
/// `timestamptz` (never formatted strings), everything else as text.
#[derive(Debug, Clone, PartialEq)]
enum DocParam {
    Text(String),
    Date(DateTime<Utc>),
}

/// Bind one [`DocParam`] list positionally, in placeholder order.
fn bind_doc_rows<'a>(
    mut query: sqlx::query::QueryAs<'a, sqlx::Postgres, DocumentRow, sqlx::postgres::PgArguments>,
    params: &'a [DocParam],
) -> sqlx::query::QueryAs<'a, sqlx::Postgres, DocumentRow, sqlx::postgres::PgArguments> {
    for param in params {
        query = match param {
            DocParam::Text(text) => query.bind(text),
            DocParam::Date(date) => query.bind(date),
        };
    }
    query
}

/// Bind one [`DocParam`] list positionally for count queries.
fn bind_doc_count<'a>(
    mut query: sqlx::query::QueryScalar<'a, sqlx::Postgres, i64, sqlx::postgres::PgArguments>,
    params: &'a [DocParam],
) -> sqlx::query::QueryScalar<'a, sqlx::Postgres, i64, sqlx::postgres::PgArguments> {
    for param in params {
        query = match param {
            DocParam::Text(text) => query.bind(text),
            DocParam::Date(date) => query.bind(date),
        };
    }
    query
}

/// The Python `_apply_filter` as SQL text plus ordered binds: document types
/// (`IN`), date bounds, language equality, and an unescaped `ILIKE`
/// `%pattern%` on `source`.
///
/// Like the Python branch, `metadata` contributes no clause: mirroring the
/// source keeps statement equivalence, and callers filter metadata through
/// the passage layer instead.
fn filtered_select(filter: &DocumentFilter, count: bool) -> (String, Vec<DocParam>) {
    let mut sql = if count {
        "SELECT count(*) FROM core.documents".to_string()
    } else {
        "SELECT id, title, document_type, language, source, content_hash, \
         parser, parser_version, ingested_at, created_date_start, \
         created_date_end, created_precision, edition_id, metadata \
         FROM core.documents"
            .to_string()
    };
    let mut clauses: Vec<String> = Vec::new();
    let mut params: Vec<DocParam> = Vec::new();
    if let Some(types) = &filter.document_types {
        if !types.is_empty() {
            // `IN` over one placeholder per type, in branch order.
            let mut slots = Vec::with_capacity(types.len());
            for doc_type in types {
                params.push(DocParam::Text(doc_type.clone()));
                slots.push(format!("${}", params.len()));
            }
            clauses.push(format!("document_type IN ({})", slots.join(", ")));
        }
    }
    if let Some(start) = &filter.date_start {
        params.push(DocParam::Date(*start));
        clauses.push(format!("created_date_start >= ${}", params.len()));
    }
    if let Some(end) = &filter.date_end {
        params.push(DocParam::Date(*end));
        clauses.push(format!("created_date_end <= ${}", params.len()));
    }
    if let Some(language) = &filter.language {
        if !language.is_empty() {
            params.push(DocParam::Text(language.clone()));
            clauses.push(format!("language = ${}", params.len()));
        }
    }
    if let Some(pattern) = &filter.source_pattern {
        if !pattern.is_empty() {
            // No LIKE escaping, exactly like the Python `ilike(f"%%{...}%%")`:
            // a `%` in the pattern stays a wildcard on both sides.
            params.push(DocParam::Text(format!("%{pattern}%")));
            clauses.push(format!("source ILIKE ${}", params.len()));
        }
    }
    if !clauses.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&clauses.join(" AND "));
    }
    (sql, params)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Mirrors `tests/unit/adapters/test_repository_surface.py` for
    // `PGDocumentRepo`: every expected method must exist on this type with a
    // compatible shape. Each `let _ = ...` path fails to compile if the
    // method is missing or its signature drifted.
    #[test]
    fn repository_exposes_expected_methods() {
        let _ = <PgDocumentRepo as DocumentRepo>::insert;
        let _ = <PgDocumentRepo as DocumentRepo>::get;
        let _ = <PgDocumentRepo as DocumentRepo>::get_many;
        let _ = <PgDocumentRepo as DocumentRepo>::find_by_hash;
        let _ = <PgDocumentRepo as DocumentRepo>::find_by_edition_id;
        let _ = <PgDocumentRepo as DocumentRepo>::update_metadata;
        let _ = <PgDocumentRepo as DocumentRepo>::iter_by_filter;
        let _ = <PgDocumentRepo as DocumentRepo>::count;
        let _ = <PgDocumentRepo as DocumentRepo>::delete;
        let _ = PgDocumentRepo::find_by_metadata;
        let _ = PgDocumentRepo::new;
    }

    fn sample_row() -> DocumentRow {
        DocumentRow {
            id: Uuid::now_v7(),
            title: Some("Title".to_string()),
            document_type: "article".to_string(),
            language: Some("en".to_string()),
            source: "src".to_string(),
            content_hash: vec![0xde, 0xad, 0xbe, 0xef],
            parser: "p".to_string(),
            parser_version: "1".to_string(),
            ingested_at: Utc::now(),
            created_date_start: None,
            created_date_end: None,
            created_precision: None,
            edition_id: None,
            metadata: Some(serde_json::json!({"author": "A"})),
        }
    }

    #[test]
    fn row_mapping_carries_every_column() {
        let id = Uuid::now_v7();
        let mut row = sample_row();
        row.id = id;
        let doc = document_from_row(row);
        assert_eq!(doc.id, id);
        assert_eq!(doc.title.as_deref(), Some("Title"));
        assert_eq!(doc.document_type, "article");
        assert_eq!(doc.content_hash, vec![0xde, 0xad, 0xbe, 0xef]);
        assert_eq!(doc.edition_id, None);
        assert_eq!(doc.metadata.len(), 1);
    }

    #[test]
    fn row_mapping_carries_an_edition() {
        let edition = Uuid::now_v7();
        let mut row = sample_row();
        row.edition_id = Some(edition);
        assert_eq!(document_from_row(row).edition_id, Some(edition));
    }

    #[test]
    fn null_metadata_maps_to_empty_object() {
        // The Python `row.metadata or {}`: NULL (or a non-object) never
        // surfaces as a missing map.
        let mut row = sample_row();
        row.metadata = None;
        assert!(document_from_row(row).metadata.is_empty());
        let mut row = sample_row();
        row.metadata = Some(Value::Null);
        assert!(document_from_row(row).metadata.is_empty());
    }

    #[test]
    fn filter_select_applies_branches_in_order() {
        let filter = DocumentFilter {
            document_types: Some(vec!["a".to_string(), "b".to_string()]),
            language: Some("en".to_string()),
            source_pattern: Some("x".to_string()),
            ..Default::default()
        };
        let (sql, params) = filtered_select(&filter, false);
        assert!(sql.contains("document_type IN ($1, $2)"), "{sql}");
        assert!(sql.contains("language = $3"), "{sql}");
        assert!(sql.contains("source ILIKE $4"), "{sql}");
        assert_eq!(
            params,
            vec![
                DocParam::Text("a".to_string()),
                DocParam::Text("b".to_string()),
                DocParam::Text("en".to_string()),
                DocParam::Text("%x%".to_string()),
            ]
        );
    }

    #[test]
    fn filter_select_skips_empty_branches() {
        let (sql, params) = filtered_select(&DocumentFilter::default(), false);
        assert!(!sql.contains("WHERE"), "{sql}");
        assert!(params.is_empty());
        let (count_sql, _) = filtered_select(&DocumentFilter::default(), true);
        assert_eq!(count_sql, "SELECT count(*) FROM core.documents");
    }
}
