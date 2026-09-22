//! Rerankers: the remote GPU offload and the disabled-mode passthrough.
//!
//! Ports `adapters/reranker/remote_api.py` ([`RemoteReranker`]),
//! `adapters/reranker/scoring.py` ([`rank_from_scores`]), and
//! `adapters/reranker/noop.py` ([`NoopReranker`]). All HTTP goes through the
//! shared [`HttpCore`](crate::http::HttpCore); this module only maps its
//! [`HttpOutcome`](crate::http::HttpOutcome) arms to reranker errors.

use uuid::Uuid;

use crate::errors::{Error, Result};
use crate::http::{HttpCore, HttpOutcome};
use crate::wire::{HealthResponse, RerankRequest, RerankResponse};

/// Consecutive failures before the circuit opens. Lower than the embedding
/// client's because the cost of being wrong is lower: a query that skips
/// reranking still returns, so failing fast and degrading beats making every
/// search wait out a timeout while a laptop is on the wrong network.
pub const FAILURE_THRESHOLD: u32 = 2;

/// Reranking is interactive. A request that takes longer than this has already
/// failed the researcher whether or not it eventually returns.
pub const DEFAULT_TIMEOUT: f64 = 30.0;

/// Budget for the `/health` handshake, mirroring the `timeout=10.0` override
/// in `RemoteReranker.health`.
const HEALTH_TIMEOUT_SECS: f64 = 10.0;

/// A `RerankerPort` backed by a remote inference server's `/rerank`. Wire
/// bodies live in [`crate::wire`], the single contract the client and server
/// share.
///
/// Failure semantics differ from the embedding client deliberately: failing
/// here raises [`Error::RerankUnavailable`] and the search service catches it,
/// returns fused results, and marks them degraded — refusing to answer a
/// question the engine could still answer reasonably well would be worse.
pub struct RemoteReranker {
    core: HttpCore,
    model_name: String,
    model_version: String,
    timeout_secs: f64,
    failure_threshold: u32,
    require_capability: bool,
    verified: bool,
    consecutive_failures: u32,
}

impl RemoteReranker {
    pub fn new(
        base_url: &str,
        model: &str,
        model_version: &str,
        timeout_secs: f64,
        bearer: Option<&str>,
        failure_threshold: u32,
        require_capability: bool,
    ) -> Self {
        Self {
            core: HttpCore::build(
                base_url,
                bearer,
                &[],
                std::time::Duration::from_secs_f64(timeout_secs.max(0.0)),
                std::time::Duration::from_secs_f64(timeout_secs.max(0.0)),
            ),
            model_name: model.to_owned(),
            model_version: model_version.to_owned(),
            timeout_secs,
            failure_threshold,
            require_capability,
            verified: false,
            consecutive_failures: 0,
        }
    }

    pub fn model_name(&self) -> &str {
        &self.model_name
    }

    pub fn model_version(&self) -> &str {
        &self.model_version
    }

    fn base_url(&self) -> &str {
        &self.core.base_url
    }

    pub async fn score(&mut self, query: &str, texts: &[String]) -> Result<Vec<f64>> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }
        // Before the handshake, not after. `_ensure_verified` dials /health, so
        // checking the circuit second meant a dead host was re-dialled on every
        // single query and the breaker never actually broke anything.
        self.check_circuit()?;
        self.ensure_verified().await?;
        // No second circuit check: every `_ensure_verified` failure path that
        // touches `consecutive_failures` returns `Err` (the `?` above
        // propagates it), and success paths never increment — so a second
        // check observes the same counter the first check passed. (`&mut self`
        // proves no concurrent query can increment between the two, unlike the
        // Python client where aliases share one counter.) The check is dead.

        // One construction site for the wire body: the shared
        // `crate::wire::RerankRequest` keeps `query, texts, expect_model` order
        // under `preserve_order`, matching `.model_dump()`. `texts` is
        // non-empty by the early return above, so `validate` cannot fail here
        // (proven: the only `Err` in `RerankRequest::validate` is the empty
        // `texts` guard); only strings cross, so `to_value` cannot fail either.
        let request = RerankRequest {
            query: query.to_owned(),
            texts: texts.to_vec(),
            expect_model: Some(self.model_name.clone()),
        };
        debug_assert!(request.validate().is_ok());
        let body = serde_json::to_value(&request).expect("strings serialize");
        let outcome = self
            .core
            .post_json("/rerank", &body, self.timeout_secs)
            .await;
        match outcome {
            HttpOutcome::Timeout(_) => {
                // Must precede Transport, which it subclasses in httpx
                // (`TimeoutException(TransportError)`, `httpx/_exceptions.py:132`
                // under `:123`). Worth separating because the two have different
                // fixes and look identical otherwise: a timeout means the server
                // is there and too slow (a CPU-only host reranking 30 candidates
                // takes ~26 s against a 30 s budget), while unreachable means it
                // is not there at all.
                self.consecutive_failures += 1;
                Err(Error::RerankUnavailable(format!(
                    "Rerank server {} did not answer within {}s. It is running but too slow for an interactive query — check it has a working accelerator, or raise RE_RERANKER_TIMEOUT if you would rather wait.",
                    self.base_url(),
                    fmt_g(self.timeout_secs)
                )))
            }
            HttpOutcome::Transport(describe) => {
                self.consecutive_failures += 1;
                Err(Error::RerankUnavailable(format!(
                    "Cannot reach the rerank server at {}: {describe}",
                    self.base_url()
                )))
            }
            HttpOutcome::Status(code, text) => {
                // Counted first, then classified: a 409 is a model disagreement,
                // not an outage, and retrying will never fix it. Surfacing it as
                // `ModelMismatch` keeps it out of the "degrade quietly" path,
                // where a permanent misconfiguration would look like an
                // intermittent network problem forever.
                self.consecutive_failures += 1;
                if code == 409 {
                    Err(Error::ModelMismatch {
                        expected: self.model_name.clone(),
                        actual: text,
                        kind: "rerank".to_owned(),
                    })
                } else {
                    Err(Error::RerankUnavailable(format!(
                        "Rerank server {} returned {code}: {}",
                        self.base_url(),
                        head_chars(&text, 200)
                    )))
                }
            }
            HttpOutcome::Ok(value) => {
                self.consecutive_failures = 0;
                let response: RerankResponse = serde_json::from_value(value).map_err(|err| {
                    Error::Validation(format!(
                        "invalid /rerank response from {}: {err}",
                        self.base_url()
                    ))
                })?;
                if response.model_name != self.model_name {
                    return Err(Error::ModelMismatch {
                        expected: self.model_name.clone(),
                        actual: response.model_name,
                        kind: "rerank".to_owned(),
                    });
                }
                if response.scores.len() != texts.len() {
                    return Err(Error::RerankUnavailable(format!(
                        "Rerank server returned {} scores for {} texts.",
                        response.scores.len(),
                        texts.len()
                    )));
                }
                Ok(response.scores)
            }
        }
    }

    pub async fn rerank(
        &mut self,
        query: &str,
        passage_ids: &[Uuid],
        texts: &[String],
        k: usize,
    ) -> Result<Vec<(Uuid, f64)>> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }
        rank_from_scores(passage_ids, &self.score(query, texts).await?, k)
    }

    fn check_circuit(&self) -> Result<()> {
        if self.consecutive_failures >= self.failure_threshold {
            return Err(Error::RerankUnavailable(format!(
                "Rerank server {} failed {} times consecutively; skipping reranking until it recovers.",
                self.base_url(),
                self.consecutive_failures
            )));
        }
        Ok(())
    }

    /// No-op for a `reqwest` client, kept so the composition root can shut
    /// every backend down the same way the Python `closeables` do.
    pub async fn close(&self) {}

    async fn ensure_verified(&mut self) -> Result<()> {
        if self.verified {
            return Ok(());
        }
        // `&mut self` already serializes concurrent handshakes at the borrow
        // level, which is what the Python `asyncio.Lock` is for.
        let outcome = self.core.get_json("/health", HEALTH_TIMEOUT_SECS).await;
        let health: HealthResponse = match outcome {
            HttpOutcome::Ok(value) => serde_json::from_value(value).map_err(|err| {
                Error::Validation(format!(
                    "invalid /health response from {}: {err}",
                    self.base_url()
                ))
            })?,
            HttpOutcome::Status(code, text) => {
                // The Python handshake only catches `TransportError` here, so
                // a status error propagates uncounted. Same: no increment.
                return Err(Error::RerankUnavailable(format!(
                    "Rerank server {} returned {code}: {}",
                    self.base_url(),
                    head_chars(&text, 200)
                )));
            }
            HttpOutcome::Timeout(describe) | HttpOutcome::Transport(describe) => {
                // `TimeoutException` subclasses `TransportError`, so the
                // Python handshake lands here for a slow `/health` too —
                // same powered-on message either way.
                self.consecutive_failures += 1;
                return Err(Error::RerankUnavailable(format!(
                    "Cannot reach the rerank server at {}: {describe}. Check the host is powered on and `research-engine embed-server` is running.",
                    self.base_url()
                )));
            }
        };
        match health.rerank_model {
            None => {
                // Not an outage: the server is up and deliberately — or, more
                // often, accidentally — embedding-only. Which error depends on
                // what the operator asked for, and getting this wrong bricked
                // every search against a server that predated /rerank:
                //
                //   remote_api — "use that host". A host that cannot do the job
                //     contradicts an explicit instruction, so it is an error.
                //   auto       — "offload if you can". You cannot, so degrade
                //     and say why, the same as any other reason reranking is
                //     unavailable.
                let message = format!(
                    "The inference server at {} serves embeddings but not reranking. Restart it with `--rerank-model {}`, or set RE_RERANKER_PROVIDER=local_bge to rerank on this machine.",
                    self.base_url(),
                    self.model_name
                );
                if self.require_capability {
                    return Err(Error::Config(message));
                }
                self.consecutive_failures = self.failure_threshold;
                return Err(Error::RerankUnavailable(message));
            }
            Some(model) if model != self.model_name => {
                return Err(Error::ModelMismatch {
                    expected: self.model_name.clone(),
                    actual: model,
                    kind: "rerank".to_owned(),
                });
            }
            Some(_) => {}
        }
        self.verified = true;
        Ok(())
    }
}

/// Order passages by score, highest first, keeping the top `k`.
///
/// Shared by the local and remote rerankers so the two cannot disagree about
/// ordering. Ties keep their input order: the input arrives in fusion order, so
/// an exact tie falls back to what RRF thought — a real signal — rather than to
/// whatever order the ids happened to hash into. Python's `sorted` is stable,
/// and so is Rust's `sort_by`, so the same descending comparator reproduces the
/// same order.
pub fn rank_from_scores(
    passage_ids: &[Uuid],
    scores: &[f64],
    k: usize,
) -> Result<Vec<(Uuid, f64)>> {
    if passage_ids.len() != scores.len() {
        return Err(Error::Validation(format!(
            "Rerank returned {} scores for {} passages. Scores are positional, so a length mismatch means the pairing is wrong and every result would be mis-attributed.",
            scores.len(),
            passage_ids.len()
        )));
    }
    let mut ranked: Vec<(Uuid, f64)> = passage_ids
        .iter()
        .copied()
        .zip(scores.iter().copied())
        .collect();
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    ranked.truncate(k);
    Ok(ranked)
}

/// Passthrough reranker for `RE_RERANKER_PROVIDER=none`: returns inputs
/// unchanged, first `k`, with the same `1.0 - i * 0.01` placeholder scores the
/// Python `NoopReranker` emits (same operation order, so bit-identical).
pub struct NoopReranker;

impl NoopReranker {
    pub fn rerank(
        &self,
        _query: &str,
        passage_ids: &[Uuid],
        _texts: &[String],
        k: usize,
    ) -> Vec<(Uuid, f64)> {
        passage_ids
            .iter()
            .take(k)
            .enumerate()
            .map(|(i, id)| (*id, 1.0 - i as f64 * 0.01))
            .collect()
    }
}

/// First `max` chars, boundary-safe. The Python `text[:200]` slices chars, and
/// byte-slicing a `str` at a non-boundary would panic in Rust; resolving the
/// boundary through `char_indices` keeps the prefix verbatim without copying
/// when the body is already short.
fn head_chars(text: &str, max: usize) -> &str {
    match text.char_indices().nth(max) {
        Some((idx, _)) => &text[..idx],
        None => text,
    }
}

/// Replicates Python `f"{x:g}"` (6 significant digits, stripped) for the
/// timeout in the slow-server message — `30.0` renders `30`, `0.25` renders
/// `0.25` — so the message stays byte-identical to the Python one.
fn fmt_g(value: f64) -> String {
    if !value.is_finite() {
        return if value.is_nan() {
            "nan".to_owned()
        } else if value.is_sign_negative() {
            "-inf".to_owned()
        } else {
            "inf".to_owned()
        };
    }
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0".to_owned()
        } else {
            "0".to_owned()
        };
    }
    let exponent = value.abs().log10().floor() as i32;
    if !(-4..6).contains(&exponent) {
        let scientific = format!("{value:.5e}");
        let (mantissa, exp) = scientific
            .split_once('e')
            .expect("scientific notation always carries an exponent");
        let mantissa = mantissa.trim_end_matches('0').trim_end_matches('.');
        let (sign, digits) = match exp.strip_prefix('-') {
            Some(rest) => ("-", rest),
            _ => ("+", exp.strip_prefix('+').unwrap_or(exp)),
        };
        let mut padded = digits.to_owned();
        while padded.len() < 2 {
            padded.insert(0, '0');
        }
        format!("{mantissa}e{sign}{padded}")
    } else {
        let decimals = (5 - exponent).max(0) as usize;
        let fixed = format!("{value:.decimals$}");
        let trimmed = fixed.trim_end_matches('0').trim_end_matches('.');
        // Proven non-empty and never "-0": this branch runs only for finite
        // non-zero `value` with `exponent in -4..6`, i.e. `1e-4 <= |value| <
        // 1e6`. `decimals = max(0, 5 - exponent)` resolves `1e-9` or coarser,
        // far below the smallest magnitude here (`1e-4`), so rounding to
        // `decimals` places cannot zero the value and trimming a non-zero
        // fixed rendering cannot yield `""` or `"-0"`. (The zero value returns
        // above; smaller magnitudes take the scientific branch.)
        trimmed.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::test_support::{closed_port_base_url, spawn_stub};
    use std::sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    };

    const MODEL: &str = "BAAI/bge-reranker-v2-m3";

    fn health_body(model: &str) -> String {
        format!(
            r#"{{"status":"ok","model_name":"BAAI/bge-m3","model_version":"1.0","dim":1024,"device":"cuda:0","warm":true,"concurrency":1,"rerank_model":{model},"rerank_model_version":"1.0","rerank_warm":true}}"#
        )
    }

    fn client_for(
        base: &str,
        timeout: f64,
        require_capability: bool,
        failure_threshold: u32,
    ) -> RemoteReranker {
        RemoteReranker::new(
            base,
            MODEL,
            "1.0",
            timeout,
            None,
            failure_threshold,
            require_capability,
        )
    }

    fn rerank_ok_body(scores: &[f64]) -> String {
        format!(
            r#"{{"scores":[{}],"model_name":"{MODEL}","model_version":"1.0"}}"#,
            scores
                .iter()
                .map(|score| score.to_string())
                .collect::<Vec<_>>()
                .join(",")
        )
    }

    #[test]
    fn reranker_rank_orders_by_score_descending() {
        let (a, b, c) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        assert_eq!(
            rank_from_scores(&[a, b, c], &[0.1, 0.9, 0.5], 3).expect("rank"),
            vec![(b, 0.9), (c, 0.5), (a, 0.1)]
        );
    }

    #[test]
    fn reranker_rank_ties_keep_fusion_order_and_k_truncates() {
        let (a, b, c) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
        assert_eq!(
            rank_from_scores(&[a, b], &[0.7, 0.7], 2).expect("rank"),
            vec![(a, 0.7), (b, 0.7)]
        );
        assert_eq!(
            rank_from_scores(&[a, b, c], &[0.3, 0.9, 0.5], 2).expect("rank"),
            vec![(b, 0.9), (c, 0.5)]
        );
    }

    #[test]
    fn reranker_rank_length_mismatch_is_refused() {
        let ids = vec![Uuid::new_v4(), Uuid::new_v4()];
        let err = rank_from_scores(&ids, &[0.5], 2).expect_err("must refuse");
        assert_eq!(
            err.to_string(),
            "Rerank returned 1 scores for 2 passages. Scores are positional, so a length mismatch means the pairing is wrong and every result would be mis-attributed."
        );
    }

    #[test]
    fn reranker_noop_returns_first_k_with_stepped_scores() {
        let ids: Vec<Uuid> = (0..4).map(|_| Uuid::new_v4()).collect();
        let texts = vec!["a".to_owned(); 4];
        assert_eq!(
            NoopReranker.rerank("q", &ids, &texts, 2),
            vec![(ids[0], 1.0), (ids[1], 0.99)]
        );
        assert_eq!(NoopReranker.rerank("q", &ids, &texts, 9).len(), 4);
        assert!(NoopReranker.rerank("q", &[], &[], 3).is_empty());
    }

    #[test]
    fn reranker_fmt_g_matches_python_g() {
        for (value, expected) in [
            (30.0, "30"),
            (10.0, "10"),
            (0.5, "0.5"),
            (2.5, "2.5"),
            (0.25, "0.25"),
            (123.456, "123.456"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1_234_567.0, "1.23457e+06"),
            (123_456_789.0, "1.23457e+08"),
            (0.0, "0"),
            (1.0, "1"),
            (100.0, "100"),
            (-2.5, "-2.5"),
            (-0.0, "-0"),
        ] {
            assert_eq!(fmt_g(value), expected, "for {value}");
        }
        assert_eq!(fmt_g(f64::INFINITY), "inf");
        assert_eq!(fmt_g(f64::NEG_INFINITY), "-inf");
        assert_eq!(fmt_g(f64::NAN), "nan");
    }

    #[test]
    fn reranker_head_chars_is_boundary_safe() {
        assert_eq!(head_chars("abcdef", 200), "abcdef");
        assert_eq!(head_chars("abcdef", 3), "abc");
        let text = format!("{}tail", "é".repeat(150));
        let head = head_chars(&text, 200);
        assert!(head.starts_with(&"é".repeat(150)));
        assert!(head.is_char_boundary(head.len()));
    }

    #[tokio::test]
    async fn reranker_happy_path_ranks_by_score_and_verifies_once() {
        let health_calls = Arc::new(AtomicUsize::new(0));
        let probe = Arc::clone(&health_calls);
        let base = spawn_stub(Arc::new(move |path, _, _| {
            if path == "/health" {
                probe.fetch_add(1, Ordering::SeqCst);
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            assert_eq!(path, "/rerank");
            (200, vec![], rerank_ok_body(&[0.2, 0.9]))
        }));
        let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
        let mut remote = client_for(&base, 5.0, true, FAILURE_THRESHOLD);
        let ranked = remote
            .rerank("q", &[a, b], &["x".to_owned(), "y".to_owned()], 2)
            .await
            .expect("rank");
        assert_eq!(ranked, vec![(b, 0.9), (a, 0.2)]);
        remote
            .rerank("q", &[a, b], &["x".to_owned(), "y".to_owned()], 2)
            .await
            .expect("second rank");
        assert_eq!(health_calls.load(Ordering::SeqCst), 1);
        remote.close().await;
    }

    #[tokio::test]
    async fn reranker_empty_texts_short_circuit_before_any_io() {
        let fired = Arc::new(AtomicBool::new(false));
        let probe = Arc::clone(&fired);
        let base = spawn_stub(Arc::new(move |_, _, _| {
            probe.store(true, Ordering::SeqCst);
            (500, vec![], "must not be called".to_owned())
        }));
        let mut remote = client_for(&base, 5.0, true, FAILURE_THRESHOLD);
        assert_eq!(
            remote.score("q", &[]).await.expect("empty score"),
            Vec::<f64>::new()
        );
        assert!(remote
            .rerank("q", &[Uuid::new_v4()], &[], 1)
            .await
            .expect("empty rerank")
            .is_empty());
        // Neither empty call dialled: the stub must not have fired.
        assert!(!fired.load(Ordering::SeqCst));
        // A non-empty call does dial (and fails against this stub), which
        // fires the handler above so its lines count as covered.
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("stub answers 500");
        assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        assert!(fired.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn reranker_unreachable_host_reports_cannot_reach() {
        let mut remote = client_for(&closed_port_base_url(), 2.0, true, FAILURE_THRESHOLD);
        let err = remote
            .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
            .await
            .expect_err("must fail");
        assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        assert!(err.to_string().contains("Cannot reach the rerank server"));
        assert!(err.to_string().contains("powered on"));
    }

    #[tokio::test]
    async fn reranker_slow_server_reports_slow_not_missing() {
        let base = spawn_stub(Arc::new(|path, _, _| {
            if path == "/health" {
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            std::thread::sleep(std::time::Duration::from_millis(1500));
            (200, vec![], rerank_ok_body(&[1.0]))
        }));
        let mut remote = client_for(&base, 0.25, true, FAILURE_THRESHOLD);
        let err = remote
            .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
            .await
            .expect_err("must time out");
        assert_eq!(
            err.to_string(),
            format!("Rerank server {base} did not answer within 0.25s. It is running but too slow for an interactive query — check it has a working accelerator, or raise RE_RERANKER_TIMEOUT if you would rather wait.")
        );
    }

    #[tokio::test]
    async fn reranker_embedding_only_server_errors_when_remote_demanded() {
        let base = spawn_stub(Arc::new(|_, _, _| (200, vec![], health_body("null"))));
        let mut remote = client_for(&base, 5.0, true, FAILURE_THRESHOLD);
        let err = remote
            .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
            .await
            .expect_err("must refuse");
        assert_eq!(
            err.to_string(),
            format!(
                "The inference server at {base} serves embeddings but not reranking. Restart it with `--rerank-model {MODEL}`, or set RE_RERANKER_PROVIDER=local_bge to rerank on this machine."
            )
        );
    }

    #[tokio::test]
    async fn reranker_embedding_only_server_degrades_under_auto_and_trips() {
        let health_calls = Arc::new(AtomicUsize::new(0));
        let probe = Arc::clone(&health_calls);
        let base = spawn_stub(Arc::new(move |_, _, _| {
            probe.fetch_add(1, Ordering::SeqCst);
            (200, vec![], health_body("null"))
        }));
        let mut remote = client_for(&base, 5.0, false, FAILURE_THRESHOLD);
        let err = remote
            .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
            .await
            .expect_err("must degrade");
        assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        assert!(err.to_string().contains("--rerank-model"));
        // The capability failure trips the breaker, so the next query fails
        // fast without re-dialling `/health`.
        let err = remote
            .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
            .await
            .expect_err("must stay open");
        assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        assert!(err.to_string().contains("consecutively"));
        assert_eq!(health_calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn reranker_wrong_model_is_refused() {
        // The handshake fails before any `/rerank` is posted, so the stub
        // answers `/health` unconditionally: a `/rerank` branch here would be
        // dead (the client never dials it).
        let base = spawn_stub(Arc::new(|_, _, _| {
            (200, vec![], health_body(r#""other-model""#))
        }));
        let mut remote = client_for(&base, 5.0, true, FAILURE_THRESHOLD);
        let err = remote
            .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
            .await
            .expect_err("must refuse");
        assert_eq!(
            format!("{err:?}"),
            "ModelMismatch { expected: \"BAAI/bge-reranker-v2-m3\", actual: \"other-model\", kind: \"rerank\" }"
        );
    }

    #[tokio::test]
    async fn reranker_conflict_maps_body_to_mismatch_after_counting() {
        let base = spawn_stub(Arc::new(|path, _, _| {
            if path == "/health" {
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            (409, vec![], "expecting other-model".to_owned())
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("must mismatch");
        assert_eq!(
            format!("{err:?}"),
            "ModelMismatch { expected: \"BAAI/bge-reranker-v2-m3\", actual: \"expecting other-model\", kind: \"rerank\" }"
        );
    }

    #[tokio::test]
    async fn reranker_other_status_truncates_the_body_to_200_chars() {
        let base = spawn_stub(Arc::new(|path, _, _| {
            if path == "/health" {
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            (500, vec![], "e".repeat(300))
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("must fail");
        assert_eq!(
            err.to_string(),
            format!("Rerank server {base} returned 500: {}", "e".repeat(200))
        );
    }

    #[tokio::test]
    async fn reranker_count_and_model_mismatches_in_answers() {
        let base = spawn_stub(Arc::new(|path, _, _| {
            if path == "/health" {
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            (200, vec![], rerank_ok_body(&[0.1, 0.2, 0.3]))
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned(), "y".to_owned()])
            .await
            .expect_err("must fail");
        assert_eq!(
            err.to_string(),
            "Rerank server returned 3 scores for 2 texts."
        );

        let base = spawn_stub(Arc::new(|path, _, _| {
            if path == "/health" {
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            (
                200,
                vec![],
                r#"{"scores":[0.5],"model_name":"other-model","model_version":"1.0"}"#.to_owned(),
            )
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("must mismatch");
        assert_eq!(
            format!("{err:?}"),
            "ModelMismatch { expected: \"BAAI/bge-reranker-v2-m3\", actual: \"other-model\", kind: \"rerank\" }"
        );

        let base = spawn_stub(Arc::new(|path, _, _| {
            if path == "/health" {
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            (200, vec![], r#"{"nope":true}"#.to_owned())
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("must fail");
        assert!(format!("{err:?}").starts_with("Validation("));
    }

    #[tokio::test]
    async fn reranker_unhealthy_status_does_not_count_and_circuit_opens() {
        let base = spawn_stub(Arc::new(|_, _, _| (500, vec![], "warming up".to_owned())));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("must fail");
        assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        assert!(err.to_string().contains("returned 500"));

        let mut remote = client_for(&closed_port_base_url(), 2.0, true, 2);
        for _ in 0..2 {
            let err = remote
                .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
                .await
                .expect_err("must fail");
            assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        }
        // Stops dialling once the circuit is open, rather than paying the
        // connect timeout on every single search.
        for _ in 0..2 {
            let err = remote
                .rerank("q", &[Uuid::new_v4()], &["x".to_owned()], 1)
                .await
                .expect_err("must stay open");
            assert!(format!("{err:?}").starts_with("RerankUnavailable("));
            assert!(err.to_string().contains("consecutively"));
        }
    }

    #[tokio::test]
    async fn reranker_post_transport_is_reported_as_cannot_reach() {
        // The handshake succeeds, then `/rerank` redirect-loops until the
        // shared core exhausts its 20-redirect policy. That exhaustion is
        // neither a timeout nor a connect failure, so `classify` answers
        // `Transport` — firing the `score` transport arm, which the
        // unreachable-host test never reaches (it fails in the handshake).
        // Mirrors the Python `except TransportError` branch after a good
        // `/health`.
        let base = spawn_stub(Arc::new(|path, _, _| {
            if path == "/health" {
                return (200, vec![], health_body(&format!(r#""{MODEL}""#)));
            }
            (
                302,
                vec![("Location".to_owned(), "/loop".to_owned())],
                String::new(),
            )
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("redirect loop must fail");
        assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        assert!(err.to_string().contains("Cannot reach the rerank server"));
    }

    #[tokio::test]
    async fn reranker_slow_health_reports_cannot_reach() {
        // A `/health` that never answers within the 10 s handshake budget:
        // `TimeoutException` subclasses `TransportError` in httpx, so the
        // Python handshake lands in the same `except TransportError` branch
        // as an unreachable host — firing the `Timeout` side of the
        // `Timeout | Transport` arm (the unreachable-host test only fires
        // `Transport`). The stub sleeps past every client budget; the client
        // gives up at 10 s, so the test takes ~10 s.
        let base = spawn_stub(Arc::new(|_, _, _| {
            std::thread::sleep(std::time::Duration::from_secs(30));
            (200, vec![], health_body(&format!(r#""{MODEL}""#)))
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("slow health must fail");
        assert!(format!("{err:?}").starts_with("RerankUnavailable("));
        assert!(err.to_string().contains("Cannot reach the rerank server"));
        assert!(err.to_string().contains("powered on"));
    }

    #[tokio::test]
    async fn reranker_malformed_health_is_a_validation_error() {
        // A 200 `/health` that does not decode as `HealthResponse`: the
        // `from_value` `Err` maps to `Validation` (the `Ok(value)` arm's
        // `map_err`), mirroring Pydantic's `ValidationError` propagating out
        // of `health()` in the Python handshake.
        let base = spawn_stub(Arc::new(|_, _, _| {
            (200, vec![], r#"{"nope":true}"#.to_owned())
        }));
        let mut remote = client_for(&base, 5.0, true, 99);
        let err = remote
            .score("q", &["x".to_owned()])
            .await
            .expect_err("bad health must fail");
        assert!(format!("{err:?}").starts_with("Validation("));
    }

    #[tokio::test]
    async fn reranker_accessors_report_identity() {
        // Ports `RemoteReranker.model_name` / `model_version`: the handshake
        // asserts the served model against exactly these.
        let remote = client_for("http://127.0.0.1:9", 5.0, true, FAILURE_THRESHOLD);
        assert_eq!(remote.model_name(), MODEL);
        assert_eq!(remote.model_version(), "1.0");
        remote.close().await;
    }

    #[test]
    fn reranker_constants_match_python() {
        assert_eq!(FAILURE_THRESHOLD, 2);
        assert_eq!(DEFAULT_TIMEOUT, 30.0);
        assert_eq!(HEALTH_TIMEOUT_SECS, 10.0);
    }

    #[test]
    fn reranker_request_wire_order_is_query_texts_expect_model() {
        let request = RerankRequest {
            query: "q".to_owned(),
            texts: vec!["a".to_owned()],
            expect_model: Some(MODEL.to_owned()),
        };
        let value = serde_json::to_value(&request).expect("serialize");
        let keys: Vec<&str> = value
            .as_object()
            .expect("object")
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(keys, vec!["query", "texts", "expect_model"]);
    }
}
