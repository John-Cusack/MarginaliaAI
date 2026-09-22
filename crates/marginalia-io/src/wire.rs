//! Wire contract ported from `adapters/embedding/wire.py`.
//!
//! Shared by the remote inference client and server so the contract cannot
//! drift. Field names, order, and serde defaults match the Pydantic models
//! field-for-field: `HealthResponse` answers `GET /health`, `EmbedRequest` /
//! `EmbedResponse` travel `POST /embeddings`, `RerankRequest` /
//! `RerankResponse` travel `POST /rerank`.
//!
//! One deliberate asymmetry with Pydantic: Rust has no validation-on-
//! construction hook, so `texts` non-emptiness (`Field(min_length=1)`) is an
//! explicit [`EmbedRequest::validate`] / [`RerankRequest::validate`] step
//! returning [`Error::Validation`] — the server boundary must call it. The
//! contract pinned here is the variant, not Pydantic's wording (which varies
//! by engine version).

use serde::{Deserialize, Serialize};

use crate::errors::{Error, Result};

fn default_status() -> String {
    "ok".to_owned()
}

fn default_warm() -> bool {
    true
}

fn default_concurrency() -> i64 {
    1
}

/// `GET /health` — identity and readiness of the served models.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HealthResponse {
    #[serde(default = "default_status")]
    pub status: String,
    pub model_name: String,
    pub model_version: String,
    pub dim: i64,
    pub device: String,
    /// False while the model is still loading.
    #[serde(default = "default_warm")]
    pub warm: bool,
    /// How many embed requests may occupy the GPU at once.
    #[serde(default = "default_concurrency")]
    pub concurrency: i64,
    /// Reranker identity; `None` means embedding-only.
    #[serde(default)]
    pub rerank_model: Option<String>,
    #[serde(default)]
    pub rerank_model_version: Option<String>,
    #[serde(default)]
    pub rerank_warm: bool,
}

/// `POST /embeddings` — texts to embed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EmbedRequest {
    pub texts: Vec<String>,
    /// The model the *client* believes it is talking to; the server rejects a
    /// mismatch rather than silently serving different vectors.
    #[serde(default)]
    pub expect_model: Option<String>,
    #[serde(default)]
    pub expect_dim: Option<i64>,
}

impl EmbedRequest {
    /// Validating constructor: rejects empty `texts` with
    /// [`Error::Validation`], mirroring `Field(min_length=1)`.
    pub fn new(
        texts: Vec<String>,
        expect_model: Option<String>,
        expect_dim: Option<i64>,
    ) -> Result<Self> {
        let req = Self {
            texts,
            expect_model,
            expect_dim,
        };
        req.validate()?;
        Ok(req)
    }

    /// Explicit validation step for values arriving via plain `serde`
    /// deserialization, which runs no hooks.
    pub fn validate(&self) -> Result<()> {
        if self.texts.is_empty() {
            return Err(Error::Validation(
                "texts must contain at least one text".to_owned(),
            ));
        }
        Ok(())
    }
}

/// `POST /embeddings` response.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EmbedResponse {
    pub embeddings: Vec<Vec<f64>>,
    pub model_name: String,
    pub model_version: String,
    pub dim: i64,
}

/// `POST /rerank` — score each text against the query, in input order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RerankRequest {
    pub query: String,
    pub texts: Vec<String>,
    #[serde(default)]
    pub expect_model: Option<String>,
}

impl RerankRequest {
    /// Validating constructor: rejects empty `texts` with
    /// [`Error::Validation`], mirroring `Field(min_length=1)`.
    pub fn new(query: String, texts: Vec<String>, expect_model: Option<String>) -> Result<Self> {
        let req = Self {
            query,
            texts,
            expect_model,
        };
        req.validate()?;
        Ok(req)
    }

    /// Explicit validation step for values arriving via plain `serde`
    /// deserialization, which runs no hooks.
    pub fn validate(&self) -> Result<()> {
        if self.texts.is_empty() {
            return Err(Error::Validation(
                "texts must contain at least one text".to_owned(),
            ));
        }
        Ok(())
    }
}

/// `POST /rerank` response: one score per input text, in input order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RerankResponse {
    pub scores: Vec<f64>,
    pub model_name: String,
    pub model_version: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::errors::model_mismatch_message;

    #[test]
    fn health_defaults_match_python() {
        let health: HealthResponse = serde_json::from_str(
            r#"{"model_name":"m","model_version":"1.0","dim":1024,"device":"cuda"}"#,
        )
        .unwrap();
        assert_eq!(health.status, "ok");
        assert!(health.warm);
        assert_eq!(health.concurrency, 1);
        assert_eq!(health.rerank_model, None);
        assert_eq!(health.rerank_model_version, None);
        assert!(!health.rerank_warm);
    }

    #[test]
    fn request_json_is_byte_exact() {
        // Declaration order matches the Pydantic field order, so serialized
        // bytes match `model_dump_json` key-for-key.
        let req = EmbedRequest {
            texts: vec!["hi".to_owned()],
            expect_model: None,
            expect_dim: None,
        };
        assert_eq!(
            serde_json::to_string(&req).unwrap(),
            r#"{"texts":["hi"],"expect_model":null,"expect_dim":null}"#
        );
        let req = RerankRequest {
            query: "q".to_owned(),
            texts: vec!["a".to_owned(), "b".to_owned()],
            expect_model: Some("r".to_owned()),
        };
        assert_eq!(
            serde_json::to_string(&req).unwrap(),
            r#"{"query":"q","texts":["a","b"],"expect_model":"r"}"#
        );
    }

    #[test]
    fn responses_round_trip() {
        let embed = EmbedResponse {
            embeddings: vec![vec![0.5, -1.25]],
            model_name: "m".to_owned(),
            model_version: "1.0".to_owned(),
            dim: 2,
        };
        let back: EmbedResponse =
            serde_json::from_str(&serde_json::to_string(&embed).unwrap()).unwrap();
        assert_eq!(back, embed);

        let rerank = RerankResponse {
            scores: vec![0.1, 0.9],
            model_name: "r".to_owned(),
            model_version: "2.0".to_owned(),
        };
        let back: RerankResponse =
            serde_json::from_str(&serde_json::to_string(&rerank).unwrap()).unwrap();
        assert_eq!(back, rerank);

        let health = HealthResponse {
            status: "ok".to_owned(),
            model_name: "m".to_owned(),
            model_version: "1.0".to_owned(),
            dim: 1024,
            device: "cuda".to_owned(),
            warm: true,
            concurrency: 4,
            rerank_model: Some("r".to_owned()),
            rerank_model_version: Some("2.0".to_owned()),
            rerank_warm: true,
        };
        let back: HealthResponse =
            serde_json::from_str(&serde_json::to_string(&health).unwrap()).unwrap();
        assert_eq!(back, health);
    }

    /// Variant assertion without branches (see budget's `assert_refused`).
    fn assert_validation(err: &Error) {
        assert_eq!(
            std::mem::discriminant(err),
            std::mem::discriminant(&Error::Validation(String::new()))
        );
    }

    #[test]
    fn empty_texts_rejected_as_validation() {
        assert_validation(&EmbedRequest::new(Vec::new(), None, None).unwrap_err());
        assert_validation(&RerankRequest::new("q".to_owned(), Vec::new(), None).unwrap_err());
        // Plain deserialization runs no hooks, so the boundary re-checks.
        let req: EmbedRequest = serde_json::from_str(r#"{"texts":[]}"#).unwrap();
        assert_validation(&req.validate().unwrap_err());
        let req: RerankRequest = serde_json::from_str(r#"{"query":"q","texts":[]}"#).unwrap();
        assert_validation(&req.validate().unwrap_err());
    }

    #[test]
    fn mismatch_message_without_detail_strips() {
        assert_eq!(
            model_mismatch_message("bge-m3", "other", "", "embedding"),
            "Remote embedding server serves 'other', but this corpus expects 'bge-m3'."
        );
        assert_eq!(
            Error::ModelMismatch {
                expected: "bge-m3".to_owned(),
                actual: "other".to_owned(),
                kind: "embedding".to_owned(),
            }
            .to_string(),
            "Remote embedding server serves 'other', but this corpus expects 'bge-m3'."
        );
    }

    #[test]
    fn mismatch_message_with_detail_appends() {
        assert_eq!(
            model_mismatch_message("bge-m3", "other", "refusing to store.", "rerank"),
            "Remote rerank server serves 'other', but this corpus expects 'bge-m3'. refusing to store."
        );
    }

    #[test]
    fn new_accepts_non_empty_texts() {
        // The `Ok` path of both validating constructors: a served model
        // answers its own identity, and the boundary re-check passes.
        let req = EmbedRequest::new(vec!["hi".to_owned()], Some("m".to_owned()), Some(1024))
            .expect("non-empty texts are valid");
        assert_eq!(req.texts, ["hi"]);
        req.validate().expect("deserialized form validates");

        let req = RerankRequest::new("q".to_owned(), vec!["a".to_owned()], None)
            .expect("non-empty texts are valid");
        assert_eq!(req.texts, ["a"]);
        req.validate().expect("deserialized form validates");
    }
}
