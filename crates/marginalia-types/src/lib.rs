//! Shared kernel for the shipped seams: the SDK documents the parser and
//! chunkers produce, and the error variants they raise.
//!
//! Python sources: `packages/sdk/src/research_engine_sdk/types.py`
//! (`ParsedDocument`, `PassageDraft`) and `domain/errors.py`.

pub mod errors;
pub mod sdk;

pub use errors::{Error, Result};
