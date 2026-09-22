//! Inference placement decisions: where compute runs and what an unreachable
//! host means.
//!
//! Ports the decision layer of `adapters/inference/routing.py` — [`Workload`],
//! [`resolve_mode`], [`FallbackState`], [`is_fallback_error`], and the summary
//! builders — without `torch` or any model code. `build_inference` itself stays
//! Python: it wires live embedding/reranker adapters, lazily loads the 2.3 GB
//! local models, and owns the shutdown list, which is composition, not a
//! decision.

use crate::errors::{Error, Result};

/// What a caller is doing, which is what decides the failure policy.
pub enum Workload {
    /// One short text, a person waiting. Degrading beats failing.
    Query,
    /// Corpus-wide, unattended, results are stored. Failing beats degrading,
    /// because a silent downgrade here costs days and nobody is watching.
    Bulk,
}

impl Workload {
    pub fn as_str(&self) -> &'static str {
        match self {
            Workload::Query => "query",
            Workload::Bulk => "bulk",
        }
    }
}

/// Settle a configured mode against whether a host address actually exists.
///
/// `remote_api` without an address is a contradiction the operator has to fix.
/// `auto` without one is not — it means "offload if you can", and there is
/// nothing to offload to, so it resolves to local and says so.
/// `local_bge`/`none` win over the URL's presence, so `local_bge` is an off
/// switch that works without deleting the host address.
///
/// An empty address counts as unset, mirroring the Python `if base_url:` test,
/// which is falsy for both `None` and `""`.
pub fn resolve_mode(mode: &str, base_url: Option<&str>, what: &str) -> Result<String> {
    if mode == "local_bge" || mode == "none" {
        return Ok(mode.to_owned());
    }
    if matches!(base_url, Some(base) if !base.is_empty()) {
        return Ok(mode.to_owned());
    }
    if mode == "remote_api" {
        return Err(Error::Config(format!(
            "RE_{what}_PROVIDER=remote_api but no host is configured. Set RE_INFERENCE_BASE_URL to a `research-engine embed-server`, e.g. http://john-super-server:9882 — or set the provider to 'auto' to run locally when no host is reachable.",
            // `what` is always an ASCII literal ("embedding"/"reranker"), so
            // this matches `str.upper()` exactly.
            what = what.to_ascii_uppercase()
        )));
    }
    Ok("local_bge".to_owned())
}

/// Whether the query-fallback warning has fired. The Python wrapper logs once
/// per process — a warning on every keystroke-fast query would bury the one
/// line that explains the slowdown — then builds the local model on first
/// need. The model cache itself stays in the wiring layer; this is only the
/// once-switch.
pub struct FallbackState {
    warned: bool,
}

impl FallbackState {
    pub fn new() -> Self {
        Self { warned: false }
    }

    /// True exactly once: the first call marks and reports, later calls stay
    /// silent.
    pub fn should_warn_and_mark(&mut self) -> bool {
        if self.warned {
            false
        } else {
            self.warned = true;
            true
        }
    }
}

impl Default for FallbackState {
    fn default() -> Self {
        Self::new()
    }
}

/// Only `EmbeddingUnavailable` falls back to local. A `ModelMismatch` is a
/// misconfiguration, not an outage — falling back here would hide the one
/// failure the handshake exists to catch, and hide it permanently, since a
/// mismatch never heals on its own.
pub fn is_fallback_error(err: &Error) -> bool {
    matches!(err, Error::EmbeddingUnavailable(_))
}

/// `embedding local ({model})`: the query and bulk paths share one local
/// model, and the summary names it.
pub fn embedding_local_summary(model: &str) -> String {
    format!("embedding local ({model})")
}

/// `embedding {base}, queries fall back to local`: `auto` with a host. Bulk
/// never falls back, in either remote mode — a corpus run that silently moved
/// to the laptop would finish next week.
pub fn embedding_fallback_summary(base_url: &str) -> String {
    format!("embedding {base_url}, queries fall back to local")
}

/// `embedding {base} (no fallback)`: `remote_api` with a host.
pub fn embedding_no_fallback_summary(base_url: &str) -> String {
    format!("embedding {base_url} (no fallback)")
}

/// `reranking disabled`: `RE_RERANKER_PROVIDER=none`.
pub fn rerank_disabled_summary() -> String {
    "reranking disabled".to_owned()
}

/// `reranking local ({model})`.
pub fn rerank_local_summary(model: &str) -> String {
    format!("reranking local ({model})")
}

/// `reranking {base}, skipped if unreachable`: both remote modes behave the
/// same at call time — an outage means the search returns unreranked and
/// flagged. They differ only in what happens with no URL configured, which
/// `resolve_mode` has already settled.
pub fn rerank_remote_summary(base_url: &str) -> String {
    format!("reranking {base_url}, skipped if unreachable")
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOST: &str = "http://gpu-host:9882";

    #[test]
    fn routing_workload_names_match_strenum_values() {
        assert_eq!(Workload::Query.as_str(), "query");
        assert_eq!(Workload::Bulk.as_str(), "bulk");
    }

    #[test]
    fn routing_local_mode_ignores_a_configured_host() {
        assert_eq!(
            resolve_mode("local_bge", Some(HOST), "embedding").expect("local"),
            "local_bge"
        );
        assert_eq!(
            resolve_mode("none", Some(HOST), "reranker").expect("none"),
            "none"
        );
    }

    #[test]
    fn routing_host_keeps_the_configured_mode() {
        assert_eq!(
            resolve_mode("auto", Some(HOST), "embedding").expect("auto"),
            "auto"
        );
        assert_eq!(
            resolve_mode("remote_api", Some(HOST), "embedding").expect("remote"),
            "remote_api"
        );
    }

    #[test]
    fn routing_remote_without_a_host_is_a_configuration_error() {
        for (base_url, what, provider) in [
            (None, "embedding", "EMBEDDING"),
            (None, "reranker", "RERANKER"),
            (Some(""), "embedding", "EMBEDDING"),
        ] {
            let err = resolve_mode("remote_api", base_url, what).expect_err("must fail");
            assert_eq!(
                err.to_string(),
                format!(
                    "RE_{provider}_PROVIDER=remote_api but no host is configured. Set RE_INFERENCE_BASE_URL to a `research-engine embed-server`, e.g. http://john-super-server:9882 — or set the provider to 'auto' to run locally when no host is reachable."
                ),
                "for {what:?}"
            );
        }
    }

    #[test]
    fn routing_auto_without_a_host_runs_local() {
        assert_eq!(
            resolve_mode("auto", None, "embedding").expect("auto"),
            "local_bge"
        );
        assert_eq!(
            resolve_mode("auto", Some(""), "reranker").expect("auto"),
            "local_bge"
        );
    }

    #[test]
    fn routing_fallback_warns_once_then_stays_silent() {
        let mut state = FallbackState::new();
        assert!(state.should_warn_and_mark());
        assert!(!state.should_warn_and_mark());
        assert!(!state.should_warn_and_mark());
        let mut state = FallbackState::default();
        assert!(state.should_warn_and_mark());
    }

    #[test]
    fn routing_only_unreachable_falls_back_never_mismatch() {
        assert!(is_fallback_error(&Error::EmbeddingUnavailable(
            "host is asleep".to_owned()
        )));
        assert!(!is_fallback_error(&Error::ModelMismatch {
            expected: "bge-m3".to_owned(),
            actual: "e5-large".to_owned(),
            kind: "embedding".to_owned(),
        }));
        assert!(!is_fallback_error(&Error::RerankUnavailable(
            "down".to_owned()
        )));
        assert!(!is_fallback_error(&Error::Config("bad".to_owned())));
    }

    #[test]
    fn routing_summaries_are_byte_exact() {
        assert_eq!(
            embedding_local_summary("BAAI/bge-m3"),
            "embedding local (BAAI/bge-m3)"
        );
        assert_eq!(
            embedding_fallback_summary(HOST),
            "embedding http://gpu-host:9882, queries fall back to local"
        );
        assert_eq!(
            embedding_no_fallback_summary(HOST),
            "embedding http://gpu-host:9882 (no fallback)"
        );
        assert_eq!(rerank_disabled_summary(), "reranking disabled");
        assert_eq!(
            rerank_local_summary("BAAI/bge-reranker-v2-m3"),
            "reranking local (BAAI/bge-reranker-v2-m3)"
        );
        assert_eq!(
            rerank_remote_summary(HOST),
            "reranking http://gpu-host:9882, skipped if unreachable"
        );
    }
}
