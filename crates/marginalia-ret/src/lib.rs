//! Phase 5 retrieval storage + words: verse-identity lemma lookup, passage /
//! document / text / node / span repositories over Postgres (`sqlx`), pure
//! filter and keyword-SQL builders, eval scoring, claim-context assembly, and
//! ingestion orchestration on top of the Phase 2/3 chunk/parse crates.
//!
//! Python sources: `services/words/lookup.py`,
//! `adapters/storage/postgres/repositories/{passages,document_texts,nodes,
//! spans,documents}.py`, `eval/{runner,metrics,queryset}.py`,
//! `services/argument/{claims,context,rules}.py`, and
//! `services/ingestion/{pipeline,orchestrator,structure,reindex,
//! embed_batches,text_backfill,embedding_backfill}.py`.
//!
//! Explicitly NOT here (Phase 6): embedding inference itself (process
//! boundary), reranker/LLM adapters.
//!
//! Module map (each owned by one porting pass):
//! - [`words`]: `LemmaQuery`, `LemmaResult`, `MAX_OCCURRENCES`,
//!   `english_reference`, the shared WHERE builder, occurrence folding, and
//!   aggregate shapes. Verse identity, never char spans.
//! - [`filters`]: `SUPPORTED_FILTERS`, `validate_filters`, candidate-ID SQL
//!   builder, `build_keyword_search_sql`, LIKE escaping, name matching.
//! - [`eval`]: recall/precision/reciprocal-rank/`dcg`/`ndcg_at_k`, queryset
//!   shapes, `compare` of paired runs.
//! - [`argument`]: claim-edge validation and anchor-context window slicing.
//! - [`ingest`]: chunker registry, structure reports, reindex orphans and
//!   identity checks, batch-halving depth, backfill routes and reports.
//! - [`repos`]: `sqlx` repositories implementing the `marginalia_types`
//!   port traits (`DocumentRepo`, `DocumentTextRepo`, `PassageRepo`,
//!   `SourceSpanRepo`, …).

pub mod argument;
pub mod eval;
pub mod filters;
pub mod ingest;
pub mod repos;
pub mod words;

use thiserror::Error;

/// Errors for retrieval storage and words lookups.
#[derive(Debug, Error)]
pub enum Error {
    /// A database operation failed.
    #[error("database error: {0}")]
    Database(String),
    /// A filter key or extension id has no repository branch.
    #[error("unsupported filter: {0}")]
    UnsupportedFilter(String),
    /// A query parameter is invalid (unknown regconfig, bad range, …).
    #[error("invalid query: {0}")]
    InvalidQuery(String),
    /// A row expected to exist did not.
    #[error("not found: {0}")]
    NotFound(String),
}

/// Shorthand for fallible retrieval operations.
pub type Result<T, E = Error> = std::result::Result<T, E>;
