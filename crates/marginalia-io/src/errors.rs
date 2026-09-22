//! Error taxonomy for the Phase 6 IO adapters.
//!
//! Mirrors the Python hierarchy in `domain/errors.py` (`ValidationError`,
//! `EvidenceNotFound`, `EmbeddingUnavailable`, `RerankUnavailable`, the `LLM*`
//! family), `domain/provenance.py` (`BudgetExceeded`), and
//! `adapters/embedding/wire.py` (`ModelMismatch`), plus transport errors the
//! Python raises as `httpx` exceptions (`Transport`, `Http`, `Json`) that need
//! first-class variants once the clients are Rust.
//!
//! `Display` is implemented by hand rather than `thiserror` derives: three
//! messages need computation (`BudgetExceeded` float formatting lives in
//! [`crate::budget`] as the single formatting site, `EvidenceNotFound`
//! truncates to 100 chars, `ModelMismatch` needs Python-`repr` quoting), which
//! `#[error]` attributes cannot express.

use std::fmt;

/// All errors raised by `marginalia-io`.
#[derive(Debug, Clone, PartialEq)]
pub enum Error {
    /// Configured LLM spend limit reached; the call was refused, not attempted.
    BudgetExceeded {
        spent: f64,
        limit: f64,
        window_days: i64,
    },
    /// Data validation failed (e.g. an empty `texts` list on the wire).
    Validation(String),
    /// Evidence span not found in passage text.
    EvidenceNotFound {
        field: String,
        passage_id: String,
        span_text: String,
    },
    /// The server is not serving the model this corpus was built with.
    ///
    /// Carries identity only; render detail at the raise site with
    /// [`model_mismatch_message`], which replicates the Python `__init__`.
    ModelMismatch {
        expected: String,
        actual: String,
        kind: String,
    },
    /// The embedding backend cannot be reached, and a smaller batch will not help.
    EmbeddingUnavailable(String),
    /// The reranker backend cannot be reached.
    RerankUnavailable(String),
    /// Invalid or missing configuration.
    Config(String),
    /// The server answered with an HTTP error status.
    Http { status: u16, body: String },
    /// The LLM is not configured, or will not authenticate.
    LlmUnavailable(String),
    /// LLM provider rate limit exceeded.
    LlmRateLimited(String),
    /// LLM provider is unreachable.
    LlmProviderDown(String),
    /// Any other error from an LLM provider.
    Llm(String),
    /// The request never reached the server (connect error, timeout, TLS).
    Transport(String),
    /// The body was not the JSON the wire contract defines.
    Json(String),
}

/// Replicates `ModelMismatch.__init__`: `Remote {kind} server serves
/// {actual!r}, but this corpus expects {expected!r}. {detail}`, stripped, so an
/// empty `detail` leaves a trailing period rather than trailing whitespace.
pub fn model_mismatch_message(expected: &str, actual: &str, detail: &str, kind: &str) -> String {
    format!(
        "Remote {kind} server serves {}, but this corpus expects {}. {detail}",
        py_repr(actual),
        py_repr(expected),
    )
    .trim()
    .to_owned()
}

/// Python `repr` for `str`: single quotes with `\`-escapes. Controls render
/// as `\x..` exactly like CPython; anything printable passes through
/// literally, as CPython `repr` renders it.
fn py_repr(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('\'');
    for c in s.chars() {
        match c {
            '\'' => out.push_str("\\'"),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || (0x7f..=0x9f).contains(&(c as u32)) => {
                // Proof the `\x` shape is total here: the guard admits only
                // U+0000–U+001F and U+007F–U+009F, all below 0x100, so the old
                // `\u`/`\U` fallbacks were unreachable and are removed, not
                // waived. Anything else is printable model-identity text and
                // passes through literally, as CPython `repr` renders it.
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('\'');
    out
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::BudgetExceeded {
                spent,
                limit,
                window_days,
            } => write!(
                f,
                "{}",
                crate::budget::budget_exceeded_message(*spent, *limit, *window_days)
            ),
            Error::Validation(msg) => write!(f, "{msg}"),
            Error::EvidenceNotFound {
                field,
                passage_id,
                span_text,
            } => {
                let head: String = span_text.chars().take(100).collect();
                write!(
                    f,
                    "Evidence span for field '{field}' not found in passage {passage_id}: '{head}'"
                )
            }
            Error::ModelMismatch {
                expected,
                actual,
                kind,
            } => write!(f, "{}", model_mismatch_message(expected, actual, "", kind)),
            Error::EmbeddingUnavailable(msg) => write!(f, "{msg}"),
            Error::RerankUnavailable(msg) => write!(f, "{msg}"),
            Error::Config(msg) => write!(f, "{msg}"),
            Error::Http { status, body } => write!(f, "HTTP error {status}: {body}"),
            Error::LlmUnavailable(msg) => write!(f, "{msg}"),
            Error::LlmRateLimited(msg) => write!(f, "{msg}"),
            Error::LlmProviderDown(msg) => write!(f, "{msg}"),
            Error::Llm(msg) => write!(f, "{msg}"),
            Error::Transport(msg) => write!(f, "{msg}"),
            Error::Json(msg) => write!(f, "{msg}"),
        }
    }
}

impl std::error::Error for Error {}

/// Defaulted-error-param alias, matching the sibling crates' convention.
pub type Result<T, E = Error> = std::result::Result<T, E>;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::budget_exceeded_message;

    #[test]
    fn budget_exceeded_display_is_verbatim() {
        let err = Error::BudgetExceeded {
            spent: 10.5,
            limit: 10.0,
            window_days: 30,
        };
        assert_eq!(err.to_string(), budget_exceeded_message(10.5, 10.0, 30));
        assert_eq!(
            err.to_string(),
            "LLM budget exceeded: $10.50 spent in the last 30d against a $10.00 limit. Raise RE_LLM_BUDGET_USD or wait for the window to roll over."
        );
    }

    #[test]
    fn validation_display_is_verbatim() {
        assert_eq!(
            Error::Validation("texts must contain at least one text".to_owned()).to_string(),
            "texts must contain at least one text"
        );
    }

    #[test]
    fn evidence_not_found_display_is_verbatim() {
        let err = Error::EvidenceNotFound {
            field: "quote".to_owned(),
            passage_id: "pid-1".to_owned(),
            span_text: "a sentence never written".to_owned(),
        };
        assert_eq!(
            err.to_string(),
            "Evidence span for field 'quote' not found in passage pid-1: 'a sentence never written'"
        );
    }

    #[test]
    fn evidence_not_found_display_truncates_to_100_chars() {
        let err = Error::EvidenceNotFound {
            field: "quote".to_owned(),
            passage_id: "pid-1".to_owned(),
            span_text: "z".repeat(150),
        };
        assert_eq!(
            err.to_string(),
            format!(
                "Evidence span for field 'quote' not found in passage pid-1: '{}'",
                "z".repeat(100)
            )
        );
    }

    #[test]
    fn model_mismatch_display_is_verbatim() {
        let err = Error::ModelMismatch {
            expected: "bge-m3".to_owned(),
            actual: "other".to_owned(),
            kind: "embedding".to_owned(),
        };
        assert_eq!(
            err.to_string(),
            "Remote embedding server serves 'other', but this corpus expects 'bge-m3'."
        );
        assert_eq!(
            model_mismatch_message("bge-m3", "other", "refusing to store.", "rerank"),
            "Remote rerank server serves 'other', but this corpus expects 'bge-m3'. refusing to store."
        );
    }

    #[test]
    fn model_mismatch_message_escapes_like_python_repr() {
        assert_eq!(
            model_mismatch_message(
                "bge-m3",
                "o'clock\\back\nnew\rcarriage\ttab",
                "",
                "embedding"
            ),
            r#"Remote embedding server serves 'o\'clock\\back\nnew\rcarriage\ttab', but this corpus expects 'bge-m3'."#
        );
    }

    #[test]
    fn model_mismatch_message_falls_back_like_python_repr() {
        // Controls render `\x..`; printable exotics pass through literally —
        // both exactly as CPython `repr` renders them.
        let exotic: String = [0x1bu32, 0x7f, 0x80, 0x1234, 0x1f600]
            .into_iter()
            .map(|n| char::from_u32(n).expect("test code point"))
            .collect();
        assert_eq!(
            model_mismatch_message("m", &exotic, "", "embedding"),
            r#"Remote embedding server serves '\x1b\x7f\x80ሴ😀', but this corpus expects 'm'."#
        );
    }

    #[test]
    fn passthrough_displays_are_verbatim() {
        assert_eq!(
            Error::EmbeddingUnavailable("no backend".to_owned()).to_string(),
            "no backend"
        );
        assert_eq!(
            Error::RerankUnavailable("no reranker".to_owned()).to_string(),
            "no reranker"
        );
        assert_eq!(
            Error::Config("missing RE_KEY".to_owned()).to_string(),
            "missing RE_KEY"
        );
        assert_eq!(
            Error::Http {
                status: 503,
                body: "overloaded".to_owned(),
            }
            .to_string(),
            "HTTP error 503: overloaded"
        );
        assert_eq!(
            Error::LlmUnavailable("no key".to_owned()).to_string(),
            "no key"
        );
        assert_eq!(
            Error::LlmRateLimited("slow down".to_owned()).to_string(),
            "slow down"
        );
        assert_eq!(
            Error::LlmProviderDown("no route".to_owned()).to_string(),
            "no route"
        );
        assert_eq!(Error::Llm("bad reply".to_owned()).to_string(), "bad reply");
        assert_eq!(
            Error::Transport("connection reset".to_owned()).to_string(),
            "connection reset"
        );
        assert_eq!(
            Error::Json("expected object".to_owned()).to_string(),
            "expected object"
        );
    }
}
