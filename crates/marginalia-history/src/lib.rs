//! Phase 7 history-plugin pure core: correspondent-name reduction, holdings
//! indexing, and correspondence-cadence math.
//!
//! Python sources: `packages/plugins/history/history/tools/_holdings.py`,
//! `correspondence_cadence.py` (pure bucket/timeline/anomaly math), and the
//! gap core of `find_missing_letters.py` (`_cadence` rhythm math,
//! `_count_by_method`, `RESOLVED`).
//!
//! What stays Python and why: `build` (async corpus-client reads behind the
//! plugin SDK), both `tool_handler` entry points (they query the event,
//! extraction, entity, and corpus clients through SDK types), `_referenced`
//! (extraction-record verdicts over live holdings), and `_as_uuid` /
//! `_as_datetime` (MCP-string input validation glue for the orchestration that
//! stays — the cadence entry point takes typed `EventFilter` fields, and the
//! strings it actually receives are validated at that boundary).

pub mod cadence;
pub mod holdings;
