//! Phase 1 text proof core: normalization, anchoring, token estimates,
//! span-preserving splits, section recovery, and the quote-tier decision.
//!
//! Python sources: `services/text/{normalize,anchoring,tokens,sections}.py`,
//! `services/verification/quote.py` (pure core), `sdk/chunking.py`.
//! All offsets are character offsets, exactly as Python string indices are.

pub mod anchoring;
pub mod chars;
pub mod normalize;
pub mod quote;
pub mod repr;
pub(crate) mod repr_table;
pub mod sections;
pub mod spans;
pub mod tokens;
pub mod word_table;
