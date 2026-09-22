//! `DocumentTextRepo` over Postgres (`sqlx`).
//!
//! Python source:
//! `adapters/storage/postgres/repositories/document_texts.py`. Table:
//! `core.document_texts` (`document_id`, `text`, `normalized_text`,
//! `normalization_version`, `parser`, `parser_version`, `created_at`).
//!
//! [`put`] folds with `marginalia_text::{normalize, NORMALIZATION_VERSION}`,
//! exactly like the Python `put`.

use std::collections::HashMap;

use marginalia_types::documents::DocumentText;
use marginalia_types::errors::{Error, Result};
use marginalia_types::ports::DocumentTextRepo;
use sqlx::PgPool;
use uuid::Uuid;

use super::{db_err, PgTx};

/// The repository. Holds a [`PgPool`]; [`put`] takes a [`PgTx`] opened via
/// [`PgTx::begin`].
pub struct PgDocumentTextRepo {
    pool: PgPool,
}

impl PgDocumentTextRepo {
    /// Serve the repository from an existing pool.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// One slice of a document's canonical text, sliced by the database.
    ///
    /// `substring` does the slice where the text already lives instead of
    /// reading a whole (possibly multi-megabyte) document to return a
    /// fragment. Extra method mirroring `PGDocumentTextRepo.get_span`; not
    /// on the port trait.
    ///
    /// SQL `substring` is 1-indexed and takes a length, not an end offset;
    // `start`/`length` bind as `int4`: Postgres has no
    // `substring(text, bigint, bigint)` overload, and the Python side sends
    // `INTEGER` for the same reason.
    /// the `+ 1` bridges from the 0-indexed half-open `[start, end)`.
    /// Returns `None` when the document has no stored text.
    pub async fn get_span(
        &self,
        document_id: Uuid,
        start: i64,
        end: i64,
    ) -> Result<Option<String>> {
        // Clamped rather than returned early: an empty span on a document
        // that has no text must still answer None, and Postgres rejects a
        // negative substring length outright. (`length` is `i64` here so the
        // subtraction cannot underflow before the clamp.)
        let length = (end - start).max(0);
        sqlx::query_scalar(
            "SELECT substring(text, $2, $3) FROM core.document_texts \
             WHERE document_id = $1",
        )
        .bind(document_id)
        .bind(start as i32 + 1)
        .bind(length as i32)
        .fetch_optional(&self.pool)
        .await
        .map_err(db_err)
    }

    /// Many slices, one round trip, answers in the order asked for.
    ///
    /// Extra method mirroring `PGDocumentTextRepo.get_spans`; not on the
    /// port trait. Requests are not deduplicated, and the join is an OUTER
    /// join so a document with no stored text yields `None` in its position
    /// rather than shifting every later answer onto the wrong request.
    pub async fn get_spans(&self, requests: &[(Uuid, i64, i64)]) -> Result<Vec<Option<String>>> {
        if requests.is_empty() {
            return Ok(Vec::new());
        }
        // Clamped client-side, not in SQL: Postgres `substring(t, -2, 5)` is
        // not an error — negative positions are consumed by the length, so
        // an unclamped window near offset 0 silently returns a fraction of
        // what was asked for.
        let mut indexes: Vec<i32> = Vec::with_capacity(requests.len());
        let mut doc_ids: Vec<Uuid> = Vec::with_capacity(requests.len());
        let mut starts: Vec<i32> = Vec::with_capacity(requests.len());
        let mut widths: Vec<i32> = Vec::with_capacity(requests.len());
        for (idx, (document_id, start, end)) in requests.iter().enumerate() {
            let clamped_start = (*start).max(0);
            indexes.push(idx as i32);
            doc_ids.push(*document_id);
            starts.push(clamped_start as i32);
            widths.push((end - clamped_start).max(0) as i32);
        }
        // `unnest` over parallel arrays is the single-statement form of the
        // Python `sa.values(...).data(rows)` construct: one synthetic row per
        // request, outer-joined to the texts.
        let rows: Vec<(i32, Option<String>)> = sqlx::query_as(
            "SELECT q.idx, substring(t.text, q.start_at + 1, q.width) \
             FROM unnest($1::int[], $2::uuid[], $3::int[], $4::int[]) \
             AS q(idx, document_id, start_at, width) \
             LEFT JOIN core.document_texts t ON t.document_id = q.document_id \
             ORDER BY q.idx",
        )
        .bind(&indexes)
        .bind(&doc_ids)
        .bind(&starts)
        .bind(&widths)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        let found: HashMap<i32, Option<String>> = rows.into_iter().collect();
        Ok((0..requests.len() as i32)
            .map(|idx| found.get(&idx).cloned().flatten())
            .collect())
    }

    /// Documents whose normalized text contains `needle` verbatim.
    ///
    /// Index-backed: `gin_trgm_ops` serves `LIKE '%...%'`. Extra method
    /// mirroring `PGDocumentTextRepo.find_documents_containing`; not on the
    /// port trait. `needle` must already be normalized the same way the
    /// column was.
    pub async fn find_documents_containing(&self, needle: &str, limit: i64) -> Result<Vec<Uuid>> {
        if needle.is_empty() {
            return Ok(Vec::new());
        }
        // LIKE metacharacters escaped via the shared builder, byte-identical
        // to the Python `_like_escape`.
        let pattern = format!("%{}%", crate::filters::like_escape(needle));
        sqlx::query_scalar(
            "SELECT document_id FROM core.document_texts \
             WHERE normalized_text LIKE $1 ESCAPE '\\' LIMIT $2",
        )
        .bind(pattern)
        .bind(limit)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)
    }

    /// Offset of `needle` in the raw canonical text, or `None`.
    ///
    /// Extra method mirroring `PGDocumentTextRepo.find_raw`; not on the port
    /// trait.
    pub async fn find_raw(&self, document_id: Uuid, needle: &str) -> Result<Option<i64>> {
        self.strpos("text", document_id, needle).await
    }

    /// Offset of `needle` in the normalized text, or `None`.
    ///
    /// Extra method mirroring `PGDocumentTextRepo.find_normalized`; not on
    /// the port trait.
    pub async fn find_normalized(&self, document_id: Uuid, needle: &str) -> Result<Option<i64>> {
        self.strpos("normalized_text", document_id, needle).await
    }

    /// Row count. Extra method mirroring `PGDocumentTextRepo.count`; not on
    /// the port trait.
    pub async fn count(&self) -> Result<i64> {
        sqlx::query_scalar("SELECT count(*) FROM core.document_texts")
            .fetch_one(&self.pool)
            .await
            .map_err(db_err)
    }

    /// `strpos` is 1-indexed and returns 0 for "not found"; callers want a
    /// 0-indexed offset and `None`.
    ///
    /// `column` is interpolated, never bound — but it is only ever one of
    /// the two literals above, so there is no injection surface. (Postgres
    /// has no parameter slot for an identifier.)
    async fn strpos(&self, column: &str, document_id: Uuid, needle: &str) -> Result<Option<i64>> {
        if needle.is_empty() {
            return Ok(None);
        }
        let sql = format!(
            "SELECT strpos({column}, $1) FROM core.document_texts \
             WHERE document_id = $2"
        );
        let at: Option<i32> = sqlx::query_scalar(&sql)
            .bind(needle)
            .bind(document_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(db_err)?;
        // `strpos` returns `integer`: 0 (or no row) means "not found".
        Ok(match at {
            Some(at) if at != 0 => Some(i64::from(at) - 1),
            _ => None,
        })
    }
}

impl DocumentTextRepo for PgDocumentTextRepo {
    type Tx = PgTx;

    async fn put(
        &self,
        tx: &mut Self::Tx,
        document_id: Uuid,
        text: &str,
        parser: &str,
        parser_version: &str,
    ) -> Result<()> {
        // Upsert rather than insert: re-parsing a document under a new parser
        // version replaces the substrate. The fold and its version come from
        // `marginalia_text`, the same functions the Python `put` calls.
        let normalized = marginalia_text::normalize::normalize(text);
        sqlx::query(
            "INSERT INTO core.document_texts (document_id, text, normalized_text, \
             normalization_version, parser, parser_version) \
             VALUES ($1, $2, $3, $4, $5, $6) \
             ON CONFLICT (document_id) DO UPDATE SET text = EXCLUDED.text, \
             normalized_text = EXCLUDED.normalized_text, \
             normalization_version = EXCLUDED.normalization_version, \
             parser = EXCLUDED.parser, parser_version = EXCLUDED.parser_version",
        )
        .bind(document_id)
        .bind(text)
        .bind(normalized)
        .bind(marginalia_text::normalize::NORMALIZATION_VERSION)
        .bind(parser)
        .bind(parser_version)
        .execute(tx.exec())
        .await
        .map_err(db_err)?;
        Ok(())
    }

    async fn get(&self, document_id: Uuid) -> Result<Option<DocumentText>> {
        let row: Option<TextRow> = sqlx::query_as(
            "SELECT document_id, text, normalized_text, normalization_version, \
             parser, parser_version FROM core.document_texts \
             WHERE document_id = $1",
        )
        .bind(document_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(row.map(|row| DocumentText {
            document_id: row.document_id,
            text: row.text,
            normalized_text: row.normalized_text,
            normalization_version: row.normalization_version,
            parser: row.parser,
            parser_version: row.parser_version,
        }))
    }

    async fn get_text(&self, document_id: Uuid) -> Result<Option<String>> {
        sqlx::query_scalar("SELECT text FROM core.document_texts WHERE document_id = $1")
            .bind(document_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(db_err)
    }

    async fn parser_versions(&self, document_ids: &[Uuid]) -> Result<HashMap<Uuid, String>> {
        // One query for the whole page, never one per hit. Documents with no
        // stored text are absent from the answer rather than yielding None.
        if document_ids.is_empty() {
            return Ok(HashMap::new());
        }
        let rows: Vec<(Uuid, String)> = sqlx::query_as(
            "SELECT document_id, parser_version FROM core.document_texts \
             WHERE document_id = ANY($1)",
        )
        .bind(document_ids)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(rows.into_iter().collect())
    }

    async fn lengths(&self, document_id: Uuid) -> Result<Option<(i64, i64)>> {
        // `length()` returns `integer`; the `or 0` mirrors the Python
        // `(row[0] or 0, row[1] or 0)` against a defensive NULL.
        let row: Option<(Option<i32>, Option<i32>)> = sqlx::query_as(
            "SELECT length(text), length(normalized_text) \
             FROM core.document_texts WHERE document_id = $1",
        )
        .bind(document_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(row.map(|(raw, norm)| (i64::from(raw.unwrap_or(0)), i64::from(norm.unwrap_or(0)))))
    }

    async fn missing_document_ids(&self, limit: Option<i64>) -> Result<Vec<Uuid>> {
        // `LIMIT NULL` is "no limit" in Postgres, so one statement serves
        // both the bounded and unbounded calls.
        sqlx::query_scalar(
            "SELECT d.id FROM core.documents d \
             LEFT JOIN core.document_texts t ON t.document_id = d.id \
             WHERE t.document_id IS NULL ORDER BY d.ingested_at \
             LIMIT $1",
        )
        .bind(limit)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)
    }
}

/// One `core.document_texts` row, decoded by column name.
#[derive(Debug, Clone, sqlx::FromRow)]
pub(crate) struct TextRow {
    pub document_id: Uuid,
    pub text: String,
    pub normalized_text: String,
    pub normalization_version: String,
    pub parser: String,
    pub parser_version: String,
}

/// Map a "not found" lookup to the typed error the span resolver raises.
pub(crate) fn text_not_found(document_id: Uuid) -> Error {
    Error::NotFound {
        kind: "document_text",
        id: document_id.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Mirrors `tests/unit/adapters/test_repository_surface.py` for
    // `PGDocumentTextRepo` (port methods plus the extras the surface test
    // pins: `get_span`, `get_spans`, `find_documents_containing`,
    // `find_raw`, `find_normalized`, `count`).
    #[test]
    fn repository_exposes_expected_methods() {
        let _ = <PgDocumentTextRepo as DocumentTextRepo>::put;
        let _ = <PgDocumentTextRepo as DocumentTextRepo>::get;
        let _ = <PgDocumentTextRepo as DocumentTextRepo>::get_text;
        let _ = <PgDocumentTextRepo as DocumentTextRepo>::parser_versions;
        let _ = <PgDocumentTextRepo as DocumentTextRepo>::lengths;
        let _ = <PgDocumentTextRepo as DocumentTextRepo>::missing_document_ids;
        let _ = PgDocumentTextRepo::get_span;
        let _ = PgDocumentTextRepo::get_spans;
        let _ = PgDocumentTextRepo::find_documents_containing;
        let _ = PgDocumentTextRepo::find_raw;
        let _ = PgDocumentTextRepo::find_normalized;
        let _ = PgDocumentTextRepo::count;
        let _ = PgDocumentTextRepo::new;
    }

    #[test]
    fn row_mapping_carries_every_column() {
        let row = TextRow {
            document_id: Uuid::now_v7(),
            text: "raw".to_string(),
            normalized_text: "raw".to_string(),
            normalization_version: "1.0".to_string(),
            parser: "p".to_string(),
            parser_version: "2".to_string(),
        };
        assert_eq!(row.text, "raw");
        assert_eq!(row.normalization_version, "1.0");
        assert_eq!(row.parser_version, "2");
    }

    #[test]
    fn text_not_found_names_document_text_kind() {
        let id = Uuid::now_v7();
        let err = text_not_found(id);
        // Byte-identical to the Python `NotFoundError("document_text", ...)`:
        // `f"{kind} not found: {id}"` on both sides.
        assert_eq!(err.to_string(), format!("document_text not found: {id}"));
    }
}
