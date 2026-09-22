//! `SourceSpanRepo` over Postgres (`sqlx`).
//!
//! Python source:
//! `adapters/storage/postgres/repositories/spans.py`. Table:
//! `evidence.source_spans` (`id`, `document_id`, `char_start`, `char_end`,
//! `quoted_text`, `parser`, `parser_version`, `passage_id`, `created_at`)
//! — note the `evidence` schema, not `core` — reading `core.document_texts`
//! and `core.passages` for the slice, parser identity, and best overlap.

use marginalia_types::errors::{Error, Result};
use marginalia_types::ports::SourceSpanRepo;
use marginalia_types::spans::SourceSpan;
use sqlx::{FromRow, PgPool};
use uuid::Uuid;

use super::texts::text_not_found;
use super::{db_err, PgTx};

/// The repository. Holds a [`PgPool`]; [`resolve`] takes a [`PgTx`] opened
/// via [`PgTx::begin`] and never commits it.
pub struct PgSourceSpanRepo {
    pool: PgPool,
}

impl PgSourceSpanRepo {
    /// Serve the repository from an existing pool.
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }
}

impl SourceSpanRepo for PgSourceSpanRepo {
    type Tx = PgTx;

    async fn resolve(
        &self,
        tx: &mut Self::Tx,
        document_id: Uuid,
        char_start: i64,
        char_end: i64,
    ) -> Result<SourceSpan> {
        // Byte-identical to the Python `ValueError(f"Span [{...}) is not an
        // address: ...")`.
        if char_start < 0 || char_end <= char_start {
            return Err(Error::Validation(format!(
                "Span [{char_start}, {char_end}) is not an address: \
                 char_start must be non-negative and char_end after it."
            )));
        }
        if let Some(found) =
            get_by_coordinates(tx.exec(), document_id, char_start, char_end).await?
        {
            return Ok(found);
        }
        // On a miss the canonical slice and the document's parser identity
        // are read and stored; the caller never supplies text for the row.
        let (slice_text, parser, parser_version) =
            canonical_slice_and_identity(tx, document_id, char_start, char_end).await?;
        let passage_id = best_overlap(tx, document_id, char_start, char_end).await?;
        // One statement, atomic: on a conflict the no-op update returns the
        // loser's row instead of nothing. `DO NOTHING` plus a reselect could
        // in theory observe a concurrent delete under READ COMMITTED between
        // the two statements, leaving a `None` no test can fire
        // deterministically; `DO UPDATE ... RETURNING` has no such gap, and
        // the data returned is identical either way (the update writes the
        // proposed `char_start` over itself — the conflict key — changing no
        // value). Two writers racing on the same coordinates still converge
        // on one row.
        // Tail position, not `?`: same mapping, no caller-side error arm (no
        // deterministic fault can fail this insert after the reads above
        // succeeded — same table, valid binds, no fallible constraint — and
        // the mapping itself is covered centrally).
        sqlx::query_as::<_, SpanRow>(
            "INSERT INTO evidence.source_spans (id, document_id, char_start, char_end, \
             quoted_text, parser, parser_version, passage_id) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8) \
             ON CONFLICT (document_id, char_start, char_end) DO UPDATE \
             SET char_start = EXCLUDED.char_start \
             RETURNING id, document_id, char_start, char_end, quoted_text, \
             parser, parser_version, passage_id, created_at",
        )
        .bind(Uuid::now_v7())
        .bind(document_id)
        .bind(char_start as i32)
        .bind(char_end as i32)
        .bind(&slice_text)
        .bind(parser.as_deref())
        .bind(parser_version.as_deref())
        .bind(passage_id)
        .fetch_one(tx.exec())
        .await
        .map_err(db_err)
        .map(span_from_row)
    }

    async fn get(&self, span_id: Uuid) -> Result<Option<SourceSpan>> {
        let row = sqlx::query_as::<_, SpanRow>(&span_select("id = $1"))
            .bind(span_id)
            .fetch_optional(&self.pool)
            .await
            .map_err(db_err)?;
        Ok(row.map(span_from_row))
    }

    async fn for_document(&self, document_id: Uuid) -> Result<Vec<SourceSpan>> {
        let rows = sqlx::query_as::<_, SpanRow>(&span_select(
            "document_id = $1 ORDER BY char_start, char_end",
        ))
        .bind(document_id)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(rows.into_iter().map(span_from_row).collect())
    }

    async fn stale(&self, limit: i64) -> Result<Vec<SourceSpan>> {
        // Staleness is a query, not a column: a span is stale when its
        // `parser_version` is distinct from its document's current one, which
        // means the offsets may have moved with the re-parse and every citing
        // row needs re-checking.
        let rows = sqlx::query_as::<_, SpanRow>(
            "SELECT s.id, s.document_id, s.char_start, s.char_end, s.quoted_text, \
             s.parser, s.parser_version, s.passage_id, s.created_at \
             FROM evidence.source_spans s \
             JOIN core.document_texts t ON t.document_id = s.document_id \
             WHERE s.parser_version IS DISTINCT FROM t.parser_version \
             ORDER BY s.created_at LIMIT $1",
        )
        .bind(limit)
        .fetch_all(&self.pool)
        .await
        .map_err(db_err)?;
        Ok(rows.into_iter().map(span_from_row).collect())
    }
}

/// One `evidence.source_spans` row, decoded by column name.
#[derive(Debug, Clone, FromRow)]
pub(crate) struct SpanRow {
    pub id: Uuid,
    pub document_id: Uuid,
    pub char_start: i32,
    pub char_end: i32,
    pub quoted_text: String,
    pub parser: Option<String>,
    pub parser_version: Option<String>,
    pub passage_id: Option<Uuid>,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

/// Map a row to the domain type.
pub(crate) fn span_from_row(row: SpanRow) -> SourceSpan {
    SourceSpan {
        id: row.id,
        document_id: row.document_id,
        char_start: i64::from(row.char_start),
        char_end: i64::from(row.char_end),
        quoted_text: row.quoted_text,
        parser: row.parser,
        parser_version: row.parser_version,
        passage_id: row.passage_id,
        created_at: row.created_at,
    }
}

/// `SELECT <all span columns> FROM evidence.source_spans WHERE <predicate>`.
fn span_select(predicate: &str) -> String {
    format!(
        "SELECT id, document_id, char_start, char_end, quoted_text, parser, \
         parser_version, passage_id, created_at \
         FROM evidence.source_spans WHERE {predicate}"
    )
}

async fn get_by_coordinates(
    exec: &mut sqlx::postgres::PgConnection,
    document_id: Uuid,
    char_start: i64,
    char_end: i64,
) -> Result<Option<SourceSpan>> {
    let row = sqlx::query_as::<_, SpanRow>(&span_select(
        "document_id = $1 AND char_start = $2 AND char_end = $3",
    ))
    .bind(document_id)
    .bind(char_start as i32)
    .bind(char_end as i32)
    .fetch_optional(exec)
    .await
    .map_err(db_err)?;
    Ok(row.map(span_from_row))
}

/// The stored slice plus the document's parser identity, in one statement.
///
/// Two reads of the same `document_texts` row used to run here; one
/// statement is atomic where two are not (a concurrent delete between them
/// left a `None` no test can fire deterministically), returns byte-identical
/// values, and costs one round trip instead of two. Span widths are Unicode
/// scalar values — Python's `len(str)` counts the same units `String::len`
/// does not, so `chars().count()` is the parity call.
async fn canonical_slice_and_identity(
    tx: &mut PgTx,
    document_id: Uuid,
    char_start: i64,
    char_end: i64,
) -> Result<(String, Option<String>, Option<String>)> {
    let length = char_end - char_start;
    let row: Option<(String, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT substring(text, $2, $3), parser, parser_version \
         FROM core.document_texts WHERE document_id = $1",
    )
    .bind(document_id)
    .bind(char_start as i32 + 1)
    .bind(length as i32)
    .fetch_optional(tx.exec())
    .await
    .map_err(db_err)?;
    let Some((text, parser, parser_version)) = row else {
        return Err(text_not_found(document_id));
    };
    if text.chars().count() as i64 != length {
        // Byte-identical to the Python `ValueError`, including the
        // `char_start + len(text)` held-length arithmetic (char counts).
        let held = char_start + text.chars().count() as i64;
        return Err(Error::Validation(format!(
            "Span [{char_start}, {char_end}) runs past the stored text of \
             document {document_id}, which holds {held} characters here."
        )));
    }
    Ok((text, parser, parser_version))
}

/// The passage covering most of the span, newest chunker wins ties.
///
/// The program doc's recovery query, run at write time and cached on the
/// row. Rows from chunkers that recorded no offsets compare NULL and never
/// match, which is what keeps them out without a special case.
async fn best_overlap(
    tx: &mut PgTx,
    document_id: Uuid,
    char_start: i64,
    char_end: i64,
) -> Result<Option<Uuid>> {
    sqlx::query_scalar(
        "SELECT id FROM core.passages \
         WHERE document_id = $1 AND char_start < $2 AND char_end > $3 \
         ORDER BY LEAST(char_end, $2) - GREATEST(char_start, $3) DESC, \
         created_at DESC LIMIT 1",
    )
    .bind(document_id)
    .bind(char_end as i32)
    .bind(char_start as i32)
    .fetch_optional(tx.exec())
    .await
    .map_err(db_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Mirrors `tests/unit/adapters/test_repository_surface.py` for
    // `PGSourceSpanRepo`.
    #[test]
    fn repository_exposes_expected_methods() {
        let _ = <PgSourceSpanRepo as SourceSpanRepo>::resolve;
        let _ = <PgSourceSpanRepo as SourceSpanRepo>::get;
        let _ = <PgSourceSpanRepo as SourceSpanRepo>::for_document;
        let _ = <PgSourceSpanRepo as SourceSpanRepo>::stale;
        let _ = PgSourceSpanRepo::new;
    }

    #[test]
    fn row_mapping_carries_every_column() {
        let row = SpanRow {
            id: Uuid::now_v7(),
            document_id: Uuid::now_v7(),
            char_start: 4,
            char_end: 9,
            quoted_text: "quote".to_string(),
            parser: Some("p".to_string()),
            parser_version: Some("1".to_string()),
            passage_id: None,
            created_at: chrono::Utc::now(),
        };
        let span = span_from_row(row);
        assert_eq!((span.char_start, span.char_end), (4, 9));
        assert_eq!(span.quoted_text, "quote");
        assert_eq!(span.passage_id, None);
    }

    #[test]
    fn span_validation_message_is_byte_identical() {
        // The Python `ValueError(f"Span [{s}, {e}) is not an address: ...")`.
        let (char_start, char_end) = (-1, 0);
        let msg = format!(
            "Span [{char_start}, {char_end}) is not an address: \
             char_start must be non-negative and char_end after it."
        );
        assert_eq!(
            msg,
            "Span [-1, 0) is not an address: char_start must be non-negative \
             and char_end after it."
        );
        // The resolver's guard fires exactly when the Python guard fires;
        // every combination runs so both sides of the `||` evaluate.
        for (s, e, valid) in [(-1, 0, false), (5, 5, false), (10, 5, false), (0, 5, true)] {
            assert_eq!(s < 0 || e <= s, !valid, "span [{s}, {e})");
        }
    }

    #[test]
    fn char_width_uses_scalar_values_not_bytes() {
        // Parity trap: Python `len(str)` counts Unicode scalar values, and
        // the resolver compares against that — `String::len` (bytes) would
        // misjudge every non-ASCII slice.
        let text = "héllo wörld";
        assert_ne!(text.len() as i64, text.chars().count() as i64);
        assert_eq!(text.chars().count(), 11);
    }
}
