//! Error hierarchy mirroring `domain/errors.py`.
//!
//! `ResearchEngineError` subclasses that carry no fields become unit-like
//! variants here. Message-carrying constructors (`DispatchMiss(source_ref)`,
//! `NotFoundError(kind, id)`, `BudgetExceeded(spent, limit, window_days)`,
//! `StaleWriteError`, `FrozenRevisionError`) keep their payloads as fields.

use thiserror::Error;

/// All errors of the shared kernel. Later phases add variants; existing
/// variants never change meaning.
#[derive(Debug, Error)]
pub enum Error {
    // --- Validation ---
    #[error("data validation failed: {0}")]
    Validation(String),

    #[error("evidence span not found in passage text: {0}")]
    EvidenceNotFound(String),

    #[error("a filter key reached the repository that it does not implement: {0}")]
    UnsupportedFilter(String),

    #[error("a filter extension was requested but is not registered: {0}")]
    UnknownFilterExtension(String),

    // --- Ingestion ---
    #[error("document ingestion failed: {0}")]
    Ingestion(String),

    #[error("no ingestion module matched source: {0}")]
    DispatchMiss(String),

    #[error("hinted module rejected the source: {0}")]
    Dispatch(String),

    #[error("parsing a document failed: {0}")]
    Parse(String),

    #[error("chunking a document failed: {0}")]
    Chunking(String),

    // --- LLM ---
    #[error("LLM provider error: {0}")]
    Llm(String),

    #[error("LLM provider is unreachable")]
    LlmProviderDown,

    #[error("LLM provider rate limit exceeded")]
    LlmRateLimited,

    #[error("the LLM is not configured, or will not authenticate")]
    LlmUnavailable,

    #[error("permission denied: {0}")]
    PermissionDenied(String),

    // --- Plugin ---
    #[error("plugin error: {0}")]
    Plugin(String),

    #[error("failed to load a plugin: {0}")]
    PluginLoad(String),

    #[error("two plugins register the same contribution: {0}")]
    PluginConflict(String),

    #[error("plugin configuration is missing or invalid: {0}")]
    PluginConfig(String),

    // --- Types ---
    #[error("referenced type is not registered: {0}")]
    UnknownType(String),

    // --- Embedding ---
    #[error("the embedding backend cannot be reached: {0}")]
    EmbeddingUnavailable(String),

    #[error("the reranker backend cannot be reached: {0}")]
    RerankUnavailable(String),

    // --- Storage ---
    #[error("no works directory is configured")]
    WorksNotConfigured,

    #[error("a content write aimed at a revision that is not a draft: {0}")]
    FrozenRevision(String),

    #[error("expected_updated_at did not match: someone else wrote first")]
    StaleWrite,

    #[error("database or storage error: {0}")]
    Storage(String),

    #[error("{kind} not found: {id}")]
    NotFound { kind: &'static str, id: String },

    #[error("invalid or missing configuration: {0}")]
    Configuration(String),
}

/// `describe_exception` equivalent: a description that is never empty.
pub fn describe<E: std::fmt::Display>(err: E) -> String {
    let message = err.to_string();
    if message.is_empty() {
        "unknown error".to_owned()
    } else {
        message
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
