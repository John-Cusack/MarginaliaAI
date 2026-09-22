//! `sqlx`-backed repositories implementing the `marginalia_types::ports`
//! traits 1:1 against the Python Postgres repos, plus a `DocumentNodeRepo`
//! mirroring `PGDocumentNodeRepo` (which has no port trait) and the
//! `LemmaLookup::find` execution returning [`words::LemmaResult`].
//!
//! Python sources:
//! `adapters/storage/postgres/repositories/{passages,document_texts,nodes,
//! spans,documents}.py`, `services/words/lookup.py` (execution of `find`),
//! table/column names from `adapters/storage/postgres/schema.py`.
//!
//! Pure SQL builders and decision functions are reused, never redefined:
//! [`filters`] (`build_candidate_sql`, `build_keyword_search_sql`,
//! `like_escape`, `SUPPORTED_FILTERS` validation) and [`words`]
//! (`build_where`/`totals_query`/`occurrences_query`/`aggregate_queries`
//! builders, `english_reference`, `fold_occurrences`, the `*_note`
//! constructors, `MAX_OCCURRENCES`). Language-config validation reuses
//! `marginalia_chunk::langconfig::{pg_config, is_known_config}` — the single
//! upstream table. NOTE for Main's dedup pass: `filters.rs` carries a local
//! copy of that table (`KNOWN_CONFIGS`, because pure modules stay
//! dependency-free); this module deliberately does not add a third copy and
//! reads the `marginalia-chunk` one instead.
//!
//! Submodules (one per Python repository file, plus the lemma execution):
//!
//! - [`documents`]: `PgDocumentRepo` (implements `DocumentRepo`).
//! - [`texts`]: `PgDocumentTextRepo` (implements `DocumentTextRepo`, plus the
//!   `get_span`/`get_spans`/quote-verification extras as inherent methods).
//! - [`passages`]: `PgPassageRepo` (implements `PassageRepo`, plus
//!   `get_many`/`get_by_node`/`relabel_version`/`set_locators`/`set_node_ids`
//!   as inherent methods).
//! - [`spans`]: `PgSourceSpanRepo` (implements `SourceSpanRepo`).
//! - [`nodes`]: `PgDocumentNodeRepo` (same method shapes as
//!   `PGDocumentNodeRepo`).
//! - [`lemma`]: `PgLemmaLookup` (`find`, plus `known_books` and
//!   `verse_map_is_loaded`).
//!
//! # Transactions
//!
//! Every port trait takes an associated `Tx`. Here `Tx` is always [`PgTx`]:
//! an owned `'static` [`sqlx::pool::PoolConnection`] holding one pooled
//! connection, with `BEGIN` issued at [`PgTx::begin`] and `COMMIT`/`ROLLBACK`
//! consuming it. This is deliberately *not* `sqlx::Transaction<'static>`:
//! `Transaction` borrows the pool (`Transaction<'a>` ties the handle's
//! lifetime to the borrow of the pool), so naming it in an associated type
//! without a lifetime parameter forces `Box::leak` or an `Arc` indirection.
//! `PoolConnection<Postgres>` is already an owned, `'static`, single-
//! connection handle with identical single-connection semantics, and explicit
//! `BEGIN`/`COMMIT`/`ROLLBACK` statements give the same atomicity the Python
//! `Transaction` wrapper provides. If `commit`/`rollback` is never called the
//! connection returns to the pool, where `sqlx` rolls back any open
//! transaction — the same safety net as dropping a Python transaction
//! without committing.

pub mod documents;
pub mod lemma;
pub mod nodes;
pub mod passages;
pub mod spans;
pub mod texts;

pub use documents::PgDocumentRepo;
pub use lemma::PgLemmaLookup;
pub use nodes::PgDocumentNodeRepo;
pub use passages::PgPassageRepo;
pub use spans::PgSourceSpanRepo;
pub use texts::PgDocumentTextRepo;

use marginalia_types::errors::{Error, Result};
use sqlx::{pool::PoolConnection, postgres::Postgres, PgPool};

// ---------------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------------

/// An owned `'static` transaction handle: one pooled connection with an open
/// transaction. See the module docs for why this is not
/// `sqlx::Transaction<'static>`.
///
/// Ownership rule: whoever holds the handle ends it. A method that fails
/// mid-transaction returns the error but cannot end the caller's handle, so
/// the caller must roll back before propagating (or dropping) — returning an
/// aborted handle to the pool poisons its next borrower. Methods that open
/// their own transaction (`update_metadata`, the ef-search path) roll back
/// internally for the same reason.
pub struct PgTx {
    conn: PoolConnection<Postgres>,
}

impl PgTx {
    /// Check out a connection and open a transaction on it.
    pub async fn begin(pool: &PgPool) -> Result<Self> {
        let mut conn = pool.acquire().await.map_err(db_err)?;
        sqlx::query("BEGIN")
            .execute(&mut *conn)
            .await
            .map_err(db_err)?;
        Ok(Self { conn })
    }

    /// Commit the transaction, consuming the handle.
    pub async fn commit(mut self) -> Result<()> {
        sqlx::query("COMMIT")
            .execute(&mut *self.conn)
            .await
            .map_err(db_err)?;
        Ok(())
    }

    /// Roll back the transaction, consuming the handle.
    pub async fn rollback(mut self) -> Result<()> {
        sqlx::query("ROLLBACK")
            .execute(&mut *self.conn)
            .await
            .map_err(db_err)?;
        Ok(())
    }

    /// The open connection as an executor for `sqlx::query*` calls.
    pub(crate) fn exec(&mut self) -> &mut sqlx::postgres::PgConnection {
        &mut self.conn
    }
}

/// Map any `sqlx` failure to the storage error the port traits carry.
pub(crate) fn db_err(err: sqlx::Error) -> Error {
    Error::Storage(err.to_string())
}

// ---------------------------------------------------------------------------
// Python-compatible float formatting
// ---------------------------------------------------------------------------

/// Format one `f64` the way Python's `str(float)`/`repr` does.
///
/// The vector-search binding formats the query embedding as `"[x,y,...]"`,
/// exactly like the Python `','.join(str(x) ...)`. Rust's `Display` is *not*
/// identical to Python's `repr`: `Display` never uses scientific notation
/// (`1e-05` renders `0.00001`), drops the trailing `.0` on integral floats
/// (`1.0` renders `1`), and spells NaN `NaN` instead of `nan`. Rust's `Debug`
/// (`{:?}`) is the closer match — shortest round-trip digits that always
/// keep a `.` or exponent — with one remaining difference: the exponent is
/// unsigned and unpadded (`1e16`, `1e-5`) where Python always signs and pads
/// to at least two digits (`1e+16`, `1e-05`). Both formatters are shortest-
/// round-trip (Rust's Grisu/Ryu and CPython's David Gay/Grisu emit the
/// shortest string that parses back to the same `f64`), so after exponent
/// normalisation the digits agree exactly; the differential test
/// (`tests/ret_diff.rs`, since removed after going green) proved this
/// digit-for-digit over the seeded embedding corpus, including integral,
/// subnormal-magnitude, and 17-significant-digit values.
pub fn format_float_py(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 {
            "inf".to_string()
        } else {
            "-inf".to_string()
        };
    }
    let rendered = format!("{value:?}");
    let Some(exp_pos) = rendered.find('e') else {
        return rendered;
    };
    let (mantissa, exponent) = rendered.split_at(exp_pos);
    let digits = &exponent[1..];
    let (sign, digits) = match digits.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("+", digits.strip_prefix('+').unwrap_or(digits)),
    };
    if digits.len() < 2 {
        format!("{mantissa}e{sign}0{digits}")
    } else {
        format!("{mantissa}e{sign}{digits}")
    }
}

/// Format a whole embedding the way the Python vector search does:
/// `"[" + ",".join(str(x) ...) + "]"`, bound with an explicit `::vector`
/// cast. See [`format_float_py`] for the digit-level proof.
pub fn format_vector_py(embedding: &[f64]) -> String {
    let parts: Vec<String> = embedding.iter().map(|x| format_float_py(*x)).collect();
    format!("[{}]", parts.join(","))
}

// ---------------------------------------------------------------------------
// Filter-extension bridging
// ---------------------------------------------------------------------------

/// [`crate::filters::ExtensionClause`] as a [`marginalia_types::ports`]
/// filter extension.
///
/// The port trait's `filter_candidate_ids<F: FilterExtension>` is generic
/// over the extension impl, and `F::Clause` is opaque to this crate — core
/// cannot render an unknown clause type to SQL. The binding this pass
/// establishes is: the clause type that carries SQL is
/// [`crate::filters::ExtensionClause`] (a passage-id subquery plus ordered
/// [`crate::filters::Param`] binds, with `$1`-based placeholders that
/// [`crate::filters::build_candidate_sql`] offsets on inline). Plugin impls
/// build that value and register it; `build_clause` then returns the
/// prebuilt clause unchanged — the requested `value` was already consumed
/// when the clause was constructed. `filter_id` is a constant because
/// registry identity travels in the `HashMap` key, not the value; it is only
/// read for diagnostics.
impl marginalia_types::ports::FilterExtension for crate::filters::ExtensionClause {
    type Clause = crate::filters::ExtensionClause;

    fn filter_id(&self) -> &str {
        "sql"
    }

    fn input_schema(&self) -> serde_json::Map<String, serde_json::Value> {
        serde_json::Map::new()
    }

    fn description(&self) -> &str {
        "A prebuilt passage-id subquery contributed as an ExtensionClause."
    }

    fn build_clause(
        &self,
        _value: &serde_json::Value,
    ) -> marginalia_types::errors::Result<Self::Clause> {
        Ok(self.clone())
    }
}

/// Why the generic port method cannot inline these, and where composition
/// lives instead: `F::Clause` is opaque in a generic body, and method
/// resolution there cannot dispatch on the clause type even when it happens
/// to be `ExtensionClause` (resolution is fixed at definition time, before
/// monomorphization — an autoref-specialization probe was tried and always
/// selected its fallback for exactly this reason). Rendering an arbitrary
/// clause type would need specialization (unstable) or an extra bound
/// (which narrows the port contract). Composition therefore lives in
/// [`PgPassageRepo::filter_candidate_ids_with_clauses`](passages::PgPassageRepo::filter_candidate_ids_with_clauses),
/// which takes registries of these prebuilt clauses; the generic port
/// method serves the extension-free path and fails loud otherwise.
#[cfg(test)]
mod tests {
    use super::*;
    use crate::filters::ExtensionClause;

    // --- format_float_py: digit-for-digit parity with Python repr(float) ---

    #[test]
    fn float_formatting_matches_python_repr() {
        // Each expectation is CPython `repr(x)` for the same f64.
        let cases: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-1.0, "-1.0"),
            (0.1, "0.1"),
            (0.2, "0.2"),
            (0.3, "0.3"),
            (1.5, "1.5"),
            (100.0, "100.0"),
            (std::f64::consts::PI, "3.141592653589793"),
            (0.30000000000000004, "0.30000000000000004"),
            (1e-5, "1e-05"),
            (1e-7, "1e-07"),
            (1.5e-7, "1.5e-07"),
            (1e16, "1e+16"),
            (1e21, "1e+21"),
            (1.2345678901234568e298, "1.2345678901234568e+298"),
            (5e-324, "5e-324"),
            (f64::INFINITY, "inf"),
            (f64::NEG_INFINITY, "-inf"),
            (f64::NAN, "nan"),
        ];
        for (value, expected) in cases {
            assert_eq!(format_float_py(*value), *expected, "mismatch for {value:?}");
        }
    }

    #[test]
    fn vector_formatting_matches_python_join() {
        // Python: f"[{','.join(str(x) for x in embedding)}]".
        assert_eq!(format_vector_py(&[0.1, 0.2, 0.3]), "[0.1,0.2,0.3]");
        assert_eq!(format_vector_py(&[]), "[]");
        assert_eq!(format_vector_py(&[1.0, -0.0, 1e-05]), "[1.0,-0.0,1e-05]");
    }

    // --- ExtensionClause as a FilterExtension ---

    #[test]
    fn prebuilt_clause_round_trips_through_build_clause() {
        use marginalia_types::ports::FilterExtension as _;
        let clause = ExtensionClause {
            sql: "SELECT m.passage_id FROM core.mentions m WHERE m.entity_id = $1".to_string(),
            params: vec![],
        };
        let out = clause
            .build_clause(&serde_json::Value::Null)
            .expect("prebuilt clause must return itself");
        assert_eq!(out, clause);
    }

    #[test]
    fn clause_identity_is_registry_keyed() {
        // `filter_id` is a constant because registry identity travels in
        // the `HashMap` key, not the value; it is only read for
        // diagnostics. What matters is that `build_clause` returns the
        // prebuilt clause unchanged.
        use marginalia_types::ports::FilterExtension as _;
        let clause = ExtensionClause {
            sql: "SELECT 1".to_string(),
            params: vec![],
        };
        assert_eq!(clause.filter_id(), "sql");
    }

    #[test]
    fn clause_registration_shape_is_fixed() {
        // `input_schema` is empty and `description` is the fixed diagnostic
        // string: registry identity travels in the `HashMap` key.
        use marginalia_types::ports::FilterExtension as _;
        let clause = ExtensionClause {
            sql: "SELECT 1".to_string(),
            params: vec![],
        };
        assert!(clause.input_schema().is_empty());
        assert_eq!(
            clause.description(),
            "A prebuilt passage-id subquery contributed as an ExtensionClause."
        );
    }
}
