//! Pure route decisions for `research-engine embed-server`: port of the
//! decision logic in `adapters/embedding/server.py` and the echo lines in
//! `cli/embed_server.py`.
//!
//! No framework here: the FastAPI app (semaphore, warm models,
//! `asyncio.to_thread`) stays Python. What ports is the part a misconfigured
//! deployment can get wrong — which expectation mismatches refuse with which
//! status, the exact refusal wording, and the startup banner — so both ends
//! keep failing on the first call rather than after depositing thousands of
//! incomparable vectors.

use crate::errors::{Error, Result};

/// Model served by default. Must match the client's `RE_EMBEDDING_MODEL`.
pub const MODEL: &str = "BAAI/bge-m3";
/// Vector width reported by default.
pub const DIM: i64 = 1024;
/// Bind port default.
pub const PORT: u16 = 9882;
/// Bind address default.
pub const HOST: &str = "0.0.0.0";
/// Batches allowed on the GPU at once. 1 is safest; 2 suits a 24 GB card.
pub const CONCURRENCY: i64 = 1;

/// A refused route: an HTTP status plus the exact detail the server answers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteRefusal {
    pub status: u16,
    pub detail: String,
}

impl RouteRefusal {
    pub fn new(status: u16, detail: impl Into<String>) -> Self {
        Self {
            status,
            detail: detail.into(),
        }
    }
}

impl std::fmt::Display for RouteRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "HTTP {}: {}", self.status, self.detail)
    }
}

impl std::error::Error for RouteRefusal {}

/// Check a `POST /embeddings` request's expectations against the served model.
///
/// Falsy expectations (`None`, empty model, zero dim) mean "no expectation",
/// mirroring the Python `if request.expect_model` / `if request.expect_dim`
/// guards.
pub fn check_embed_expectations(
    expect_model: Option<&str>,
    expect_dim: Option<i64>,
    served_model: &str,
    served_dim: i64,
) -> std::result::Result<(), RouteRefusal> {
    if let Some(expected) = expect_model.filter(|model| !model.is_empty()) {
        if expected != served_model {
            return Err(RouteRefusal::new(
                409,
                format!(
                    "This server serves '{served_model}'; the client expects '{expected}'. \
                     Vectors from different models are not comparable — refusing rather \
                     than serving them."
                ),
            ));
        }
    }
    if let Some(expected) = expect_dim.filter(|dim| *dim != 0) {
        if expected != served_dim {
            return Err(RouteRefusal::new(
                409,
                format!("This server serves dim {served_dim}; the client expects {expected}."),
            ));
        }
    }
    Ok(())
}

/// Check a `POST /rerank` request's expectations against the served reranker.
///
/// `None` served means the server was started without `--rerank-model`: that
/// is a deployment choice to respect (404), not an outage to report, and
/// `/health` advertises it so a client that checks the handshake first should
/// never reach here.
pub fn check_rerank_expectations(
    served_model: Option<&str>,
    expect_model: Option<&str>,
) -> std::result::Result<(), RouteRefusal> {
    let Some(served) = served_model else {
        return Err(RouteRefusal::new(
            404,
            "This server was started without --rerank-model and serves embeddings only. \
             /health advertises this, so a client that checks the handshake first should \
             never reach here.",
        ));
    };
    if let Some(expected) = expect_model.filter(|model| !model.is_empty()) {
        if expected != served {
            return Err(RouteRefusal::new(
                409,
                format!(
                    "This server reranks with '{served}'; the client expects '{expected}'. \
                     Serving it anyway would order results by a model the caller did not choose."
                ),
            ));
        }
    }
    Ok(())
}

/// Answer a backend failure: 503 rather than 500. The client's halving retry
/// is the right response to memory pressure, and a smaller batch may succeed.
pub fn backend_failure(detail: impl Into<String>) -> RouteRefusal {
    RouteRefusal::new(503, detail)
}

/// Validate a `POST /embeddings` request end to end: wire-shape validation
/// first (`texts` non-empty), then model/dim expectations.
pub fn check_embed_request(
    request: &crate::wire::EmbedRequest,
    served_model: &str,
    served_dim: i64,
) -> Result<()> {
    request.validate()?;
    check_embed_expectations(
        request.expect_model.as_deref(),
        request.expect_dim,
        served_model,
        served_dim,
    )
    .map_err(|refusal| Error::Http {
        status: refusal.status,
        body: refusal.detail,
    })
}

/// Validate a `POST /rerank` request end to end.
pub fn check_rerank_request(
    request: &crate::wire::RerankRequest,
    served_model: Option<&str>,
) -> Result<()> {
    request.validate()?;
    check_rerank_expectations(served_model, request.expect_model.as_deref()).map_err(|refusal| {
        Error::Http {
            status: refusal.status,
            body: refusal.detail,
        }
    })
}

/// The two startup echo lines `cli/embed_server.py` prints.
pub fn embed_server_banner(
    model: &str,
    dim: i64,
    host: &str,
    port: u16,
    concurrency: i64,
    rerank_model: Option<&str>,
) -> [String; 2] {
    [
        format!("Serving {model} (dim {dim}) on {host}:{port}, concurrency {concurrency}"),
        match rerank_model {
            Some(reranker) => format!("  rerank: {reranker}"),
            None => "  rerank: disabled".to_owned(),
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embed_model_mismatch_is_409_with_the_exact_wording() {
        let err = check_embed_expectations(Some("wrong-model"), Some(DIM), MODEL, DIM).unwrap_err();
        assert_eq!(err.status, 409);
        assert_eq!(
            err.detail,
            "This server serves 'BAAI/bge-m3'; the client expects 'wrong-model'. Vectors \
             from different models are not comparable — refusing rather than serving them."
        );
    }

    #[test]
    fn embed_dim_mismatch_is_409_with_the_exact_wording() {
        let err = check_embed_expectations(Some(MODEL), Some(8), MODEL, DIM).unwrap_err();
        assert_eq!(err.status, 409);
        assert_eq!(
            err.detail,
            "This server serves dim 1024; the client expects 8."
        );
    }

    #[test]
    fn embed_matching_expectations_pass() {
        assert!(check_embed_expectations(Some(MODEL), Some(DIM), MODEL, DIM).is_ok());
    }

    #[test]
    fn embed_absent_expectations_pass() {
        assert!(check_embed_expectations(None, None, MODEL, DIM).is_ok());
        // Falsy means absent, mirroring the Python truthiness guards.
        assert!(check_embed_expectations(Some(""), Some(0), MODEL, DIM).is_ok());
    }

    #[test]
    fn rerank_without_a_reranker_is_404_with_the_exact_wording() {
        let err = check_rerank_expectations(None, Some("BAAI/bge-reranker-v2-m3")).unwrap_err();
        assert_eq!(err.status, 404);
        assert_eq!(
            err.detail,
            "This server was started without --rerank-model and serves embeddings only. \
             /health advertises this, so a client that checks the handshake first should \
             never reach here."
        );
    }

    #[test]
    fn rerank_model_mismatch_is_409_with_the_exact_wording() {
        let err =
            check_rerank_expectations(Some("BAAI/bge-reranker-v2-m3"), Some("other-reranker"))
                .unwrap_err();
        assert_eq!(err.status, 409);
        assert_eq!(
            err.detail,
            "This server reranks with 'BAAI/bge-reranker-v2-m3'; the client expects \
             'other-reranker'. Serving it anyway would order results by a model the caller \
             did not choose."
        );
    }

    #[test]
    fn rerank_matching_expectations_pass() {
        assert!(check_rerank_expectations(
            Some("BAAI/bge-reranker-v2-m3"),
            Some("BAAI/bge-reranker-v2-m3")
        )
        .is_ok());
        assert!(check_rerank_expectations(Some("BAAI/bge-reranker-v2-m3"), None).is_ok());
    }

    #[test]
    fn backend_failure_is_503() {
        let refusal = backend_failure("CUDA out of memory");
        assert_eq!(refusal.status, 503);
        assert_eq!(refusal.detail, "CUDA out of memory");
    }

    #[test]
    fn banner_echoes_both_lines() {
        assert_eq!(
            embed_server_banner(
                MODEL,
                DIM,
                HOST,
                PORT,
                CONCURRENCY,
                Some("BAAI/bge-reranker-v2-m3")
            ),
            [
                "Serving BAAI/bge-m3 (dim 1024) on 0.0.0.0:9882, concurrency 1".to_owned(),
                "  rerank: BAAI/bge-reranker-v2-m3".to_owned(),
            ]
        );
        assert_eq!(
            embed_server_banner(MODEL, DIM, HOST, PORT, CONCURRENCY, None)[1],
            "  rerank: disabled"
        );
    }

    #[test]
    fn defaults_match_the_cli() {
        assert_eq!(MODEL, "BAAI/bge-m3");
        assert_eq!(DIM, 1024);
        assert_eq!(PORT, 9882);
        assert_eq!(HOST, "0.0.0.0");
        assert_eq!(CONCURRENCY, 1);
    }

    #[test]
    fn request_validators_reject_empty_texts_before_expectations() {
        let empty = crate::wire::EmbedRequest {
            texts: Vec::new(),
            expect_model: Some("wrong-model".to_owned()),
            expect_dim: None,
        };
        assert_eq!(
            check_embed_request(&empty, MODEL, DIM)
                .unwrap_err()
                .to_string(),
            "texts must contain at least one text"
        );
        let mismatched = crate::wire::EmbedRequest {
            texts: vec!["x".to_owned()],
            expect_model: Some("wrong-model".to_owned()),
            expect_dim: None,
        };
        let err = check_embed_request(&mismatched, MODEL, DIM).unwrap_err();
        assert!(err.to_string().starts_with("HTTP error 409: "));
        assert!(err.to_string().contains("not comparable"));
    }

    #[test]
    fn refusal_display_names_status_and_detail() {
        // Mirrors the FastAPI `{"detail": ...}` refusal shape: status plus the
        // exact wording, so logs name which end disagreed.
        let refusal = RouteRefusal::new(409, "mismatch");
        assert_eq!(refusal.to_string(), "HTTP 409: mismatch");
        assert_eq!(format!("{refusal}"), "HTTP 409: mismatch");
        let as_error: &dyn std::error::Error = &refusal;
        assert_eq!(as_error.to_string(), "HTTP 409: mismatch");
    }

    #[test]
    fn rerank_request_validator_rejects_empty_texts_before_expectations() {
        // Wire-shape validation first, mirroring `Field(min_length=1)`: an
        // empty `texts` fails even when the model also disagrees.
        let empty = crate::wire::RerankRequest {
            query: "q".to_owned(),
            texts: Vec::new(),
            expect_model: Some("wrong-model".to_owned()),
        };
        assert_eq!(
            check_rerank_request(&empty, Some("BAAI/bge-reranker-v2-m3"))
                .unwrap_err()
                .to_string(),
            "texts must contain at least one text"
        );
    }

    #[test]
    fn rerank_request_validator_maps_model_mismatch_to_409() {
        // Same 409 wording as `check_rerank_expectations`: the end-to-end
        // validator only translates the refusal into `Error::Http`.
        let mismatched = crate::wire::RerankRequest {
            query: "q".to_owned(),
            texts: vec!["a".to_owned()],
            expect_model: Some("other-reranker".to_owned()),
        };
        let err = check_rerank_request(&mismatched, Some("BAAI/bge-reranker-v2-m3")).unwrap_err();
        assert!(err.to_string().starts_with("HTTP error 409: "));
        assert!(err.to_string().contains("did not choose"));
    }

    #[test]
    fn rerank_request_validator_maps_missing_reranker_to_404() {
        // Embedding-only server: 404, not an outage. `/health` advertises
        // this, so a client that checks the handshake first never reaches here.
        let request = crate::wire::RerankRequest {
            query: "q".to_owned(),
            texts: vec!["a".to_owned()],
            expect_model: None,
        };
        let err = check_rerank_request(&request, None).unwrap_err();
        assert!(err.to_string().starts_with("HTTP error 404: "));
        assert!(err.to_string().contains("embeddings only"));
    }

    #[test]
    fn rerank_request_validator_passes_matching_expectations() {
        let matching = crate::wire::RerankRequest {
            query: "q".to_owned(),
            texts: vec!["a".to_owned()],
            expect_model: Some("BAAI/bge-reranker-v2-m3".to_owned()),
        };
        assert!(check_rerank_request(&matching, Some("BAAI/bge-reranker-v2-m3")).is_ok());
        let absent = crate::wire::RerankRequest {
            query: "q".to_owned(),
            texts: vec!["a".to_owned()],
            expect_model: None,
        };
        assert!(check_rerank_request(&absent, Some("BAAI/bge-reranker-v2-m3")).is_ok());
        // Falsy means absent, mirroring the Python truthiness guard.
        let falsy = crate::wire::RerankRequest {
            query: "q".to_owned(),
            texts: vec!["a".to_owned()],
            expect_model: Some(String::new()),
        };
        assert!(check_rerank_request(&falsy, Some("BAAI/bge-reranker-v2-m3")).is_ok());
    }
}
