//! Phase 0 shared kernel: validated domain models, the plugin-boundary SDK
//! DTOs, the error hierarchy, and the repository/service port traits.
//!
//! Python sources: `packages/core/src/research_engine/domain/*`,
//! `packages/core/src/research_engine/ports/*`,
//! `packages/sdk/src/research_engine_sdk/types.py`.
//!
//! Conventions carried over from the Pydantic models:
//! - Structs hold data; construction never validates. Call `validate()` to run
//!   the `model_validator` / `field_validator` rules. Deserialization enforces
//!   shape, optionality, and defaults only.
//! - Structs mirroring a model with `extra="forbid"` use
//!   `deny_unknown_fields`, so unknown JSON keys fail exactly as in Python.
//! - `bytes` fields are `Vec<u8>` (serde: array of 0-255 ints), matching what
//!   Pydantic accepts on validation.

pub mod citations;
pub mod claims;
pub mod common;
pub mod documents;
pub mod edges;
pub mod entities;
pub mod errors;
pub mod events;
pub mod extractions;
pub mod nodes;
pub mod passages;
/// Port traits use native `async fn`: internal seams (the lint's own
/// allow-if-own-code case); Phase 5 binds `Send` where executors need it.
#[allow(async_fn_in_trait)]
pub mod ports;
pub mod provenance;
pub mod sdk;
pub mod spans;
pub mod wire;
pub mod works;
pub mod works_files;
/// Port traits use native `async fn`: internal seams (the lint's own
/// allow-if-own-code case); Phase 5 binds `Send` where executors need it.
#[allow(async_fn_in_trait)]
pub mod works_ports;

pub use errors::{Error, Result};
