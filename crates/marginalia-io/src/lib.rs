//! Phase 6 IO adapters: error taxonomy, budget-guard core, and inference wire
//! contracts.
//!
//! Thin uniformity layer over out-of-process backends — no live network or
//! inference happens here, and the test suite makes no calls. Sibling modules
//! (`embedding`, `embed_server`, `llm`, `http`, `reranker`, `routing`,
//! `schemas`, `validation`, `entities`, `events`) join as later slices land;
//! each owner sends the `mod` line through the crate owner so this file stays
//! the single wiring point.

pub mod budget;
pub mod catalog;
pub mod embed_server;
pub mod embedding;
pub mod entities;
pub mod errors;
pub mod events;
pub mod http;
pub mod llm;
pub mod reranker;
pub mod routing;
pub mod schemas;
pub mod settings;
pub mod validation;
pub mod wire;
