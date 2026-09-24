//! The error variants the shipped seams raise, mirroring `domain/errors.py`.

use thiserror::Error;

/// Failures a parser or chunker reports to its caller.
#[derive(Debug, Error)]
pub enum Error {
    /// A `ValueError` on the Python side.
    #[error("data validation failed: {0}")]
    Validation(String),

    /// `ChunkingError` on the Python side.
    #[error("chunking a document failed: {0}")]
    Chunking(String),

    /// A `TypeError` on the Python side (e.g. a float used as a slice index).
    #[error("wrong type: {0}")]
    Type(String),
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
