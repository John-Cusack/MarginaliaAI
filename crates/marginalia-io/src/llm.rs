//! Anthropic + OpenAI-compatible LLM adapters over the shared [`HttpCore`].
//!
//! Ports `adapters/llm/anthropic.py` (`AnthropicLLMAdapter`) and
//! `adapters/llm/openai_compatible.py` (`OpenAICompatibleLLMAdapter`) as raw
//! `reqwest` REST via `crate::http::HttpCore`. No SDK dependencies.
//!
//! SDK defaults pinned from the installed `.venv` (proof comments inline):
//! - total timeout 600 s, connect timeout 5 s:
//!   `.venv/.../anthropic/_constants.py:9`
//!   (`DEFAULT_TIMEOUT = httpx.Timeout(timeout=10 * 60, connect=5.0)`) and
//!   `.venv/.../openai/_constants.py:9`
//!   (`DEFAULT_TIMEOUT = httpx.Timeout(timeout=600, connect=5.0)`).
//! - Anthropic base URL `https://api.anthropic.com`
//!   (`.venv/.../anthropic/_client.py:100-103`, `ANTHROPIC_BASE_URL` env
//!   override); auth header `X-Api-Key`
//!   (`.venv/.../anthropic/_client.py:160-165`); mandatory
//!   `anthropic-version: 2023-06-01`
//!   (`.venv/.../anthropic/_client.py:180`). Anthropic never uses
//!   `Authorization: Bearer` (that header only carries the OAuth
//!   `auth_token`), hence the `extra_headers` on [`HttpCore`].
//! - OpenAI base URL `https://api.openai.com/v1`
//!   (`.venv/.../openai/_client.py:160-162`); auth header
//!   `Authorization: Bearer <key>` (`.venv/.../openai/_client.py:316-320`).
//!   The adapter's `api_key or "unused"` default is ours
//!   (`openai_compatible.py:28`), not the SDK's (the SDK raises without a
//!   key).
//! - `max_tokens` defaults to 4096 in both adapters
//!   (`anthropic.py:52`, `openai_compatible.py:46`:
//!   `kwargs.get("max_tokens", 4096)`).
//!
//! The `LLMCallLogRepo.insert` calls stay Python: every method returns the
//! [`LLMCallDraft`] the Python code would insert, and every error path (save
//! the missing-key early return, which the Python also never logs) records
//! the `_log_error` draft in [`AnthropicClient::last_error_draft`] /
//! [`OpenAIClient::last_error_draft`].

use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::errors::{Error, Result};
use crate::http::{HttpCore, HttpOutcome};

/// `kwargs.get("max_tokens", 4096)` in both Python adapters.
pub const DEFAULT_MAX_TOKENS: u32 = 4096;
/// `AnthropicLLMAdapter.__init__` default (`anthropic.py:31`).
pub const ANTHROPIC_DEFAULT_MODEL: &str = "claude-sonnet-4-5-20250929";
/// `OpenAICompatibleLLMAdapter.__init__` default (`openai_compatible.py:26`).
pub const OPENAI_DEFAULT_MODEL: &str = "gpt-4o";
/// SDK default base URL (`.venv/.../anthropic/_client.py:100-103`).
pub const ANTHROPIC_BASE_URL: &str = "https://api.anthropic.com";
/// Mandatory version header (`.venv/.../anthropic/_client.py:180`).
pub const ANTHROPIC_VERSION: &str = "2023-06-01";
/// Fallback key when none is configured (`openai_compatible.py:28`:
/// `api_key or "unused"`).
pub const OPENAI_UNUSED_KEY: &str = "unused";

/// Connect timeout mirroring the SDK `connect=5.0`
/// (`.venv/.../anthropic/_constants.py:9`, `.venv/.../openai/_constants.py:9`).
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
/// Client-level request timeout mirroring the SDK `timeout=600`
/// (same two lines).
const REQUEST_TIMEOUT: Duration = Duration::from_secs(600);
/// Per-call timeout passed to `HttpCore::post_json`, same 600 s default.
const DEFAULT_CALL_TIMEOUT_SECS: f64 = 600.0;

/// Data needed to log an LLM call. Mirrors `LLMCallDraft` in
/// `domain/provenance.py:31-42` field-for-field (same names, same order;
/// token/cost/duration stays `None` until the response arrives, `status` /
/// `error` describe the outcome).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LLMCallDraft {
    pub purpose: String,
    pub caller: String,
    pub model: String,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub cost_estimate: Option<f64>,
    pub duration_ms: Option<i64>,
    pub status: String,
    pub error: Option<String>,
}

impl LLMCallDraft {
    fn ok(
        purpose: &str,
        caller: &str,
        model: &str,
        input_tokens: Option<i64>,
        output_tokens: Option<i64>,
        cost_estimate: Option<f64>,
        duration_ms: i64,
    ) -> Self {
        Self {
            purpose: purpose.to_owned(),
            caller: caller.to_owned(),
            model: model.to_owned(),
            input_tokens,
            output_tokens,
            cost_estimate,
            duration_ms: Some(duration_ms),
            status: "ok".to_owned(),
            error: None,
        }
    }

    /// Mirrors `AnthropicLLMAdapter._log_error` (`anthropic.py:177-190`): only
    /// `purpose`/`caller`/`model`/`duration_ms`/`status="error"`/`error` are
    /// set; token and cost columns stay `NULL`.
    fn error(purpose: &str, caller: &str, model: &str, duration_ms: i64, error: &str) -> Self {
        Self {
            purpose: purpose.to_owned(),
            caller: caller.to_owned(),
            model: model.to_owned(),
            input_tokens: None,
            output_tokens: None,
            cost_estimate: None,
            duration_ms: Some(duration_ms),
            status: "error".to_owned(),
            error: Some(error.to_owned()),
        }
    }
}

/// One chat message. The Python adapters pass `list[dict[str, str]]` straight
/// to the SDK; in practice every entry is `{role, content}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

impl ChatMessage {
    pub fn new(role: &str, content: &str) -> Self {
        Self {
            role: role.to_owned(),
            content: content.to_owned(),
        }
    }
}

/// `int((time.monotonic() - start) * 1000)` (`anthropic.py:54,125,180`,
/// `openai_compatible.py:48,98`): float multiplication truncated toward zero.
/// `as` saturates rather than wrapping, but elapsed times are always
/// non-negative and far below `i64::MAX` ms, so this is exactly `int()`.
pub fn trunc_ms(elapsed_secs: f64) -> i64 {
    (elapsed_secs * 1000.0) as i64
}

fn elapsed_ms(start: &Instant) -> i64 {
    trunc_ms(start.elapsed().as_secs_f64())
}

/// Rough cost estimates per 1M tokens, verbatim from
/// `AnthropicLLMAdapter._estimate_cost` (`anthropic.py:192-201`):
/// `(input * in_rate + output * out_rate) / 1_000_000`, unknown models fall
/// back to the sonnet rate. (Only the Anthropic adapter logs cost; the
/// OpenAI-compatible adapter leaves `cost_estimate=None`.)
pub fn estimate_cost(model: &str, input_tokens: i64, output_tokens: i64) -> f64 {
    let (in_rate, out_rate) = match model {
        "claude-sonnet-4-5-20250929" => (3.0, 15.0),
        "claude-haiku-4-5-20251001" => (0.80, 4.0),
        "claude-opus-4-6" => (15.0, 75.0),
        _ => (3.0, 15.0),
    };
    (input_tokens as f64 * in_rate + output_tokens as f64 * out_rate) / 1_000_000.0
}

/// Anthropic Messages API client (`POST {base}/v1/messages`).
pub struct AnthropicClient {
    http: HttpCore,
    api_key: Option<String>,
    default_model: String,
    call_timeout_secs: f64,
    last_error_draft: Option<LLMCallDraft>,
}

impl AnthropicClient {
    pub fn new(api_key: Option<&str>, default_model: Option<&str>) -> Self {
        Self::with_base_url(api_key, default_model, ANTHROPIC_BASE_URL)
    }

    /// Loopback-test entry point: same client against a stub base URL.
    pub fn with_base_url(
        api_key: Option<&str>,
        default_model: Option<&str>,
        base_url: &str,
    ) -> Self {
        let api_key = api_key.map(str::to_owned);
        // Auth headers mirror the SDK (`X-Api-Key`, never Bearer;
        // `.venv/.../anthropic/_client.py:160-165`) plus the mandatory
        // version header (`.venv/.../anthropic/_client.py:180`).
        let http = match &api_key {
            Some(key) => HttpCore::build(
                base_url,
                None,
                &[
                    ("x-api-key", key.as_str()),
                    ("anthropic-version", ANTHROPIC_VERSION),
                ],
                CONNECT_TIMEOUT,
                REQUEST_TIMEOUT,
            ),
            // No key: nothing is ever sent (see `missing_key`), so headers
            // are irrelevant; the SDK would fail header validation the same
            // way before any request.
            None => HttpCore::build(base_url, None, &[], CONNECT_TIMEOUT, REQUEST_TIMEOUT),
        };
        Self {
            http,
            api_key,
            default_model: default_model.unwrap_or(ANTHROPIC_DEFAULT_MODEL).to_owned(),
            call_timeout_secs: DEFAULT_CALL_TIMEOUT_SECS,
            last_error_draft: None,
        }
    }

    /// Override the per-call timeout (tests; default is the SDK 600 s).
    pub fn set_call_timeout(&mut self, secs: f64) {
        self.call_timeout_secs = secs;
    }

    /// The `_log_error` draft of the most recent failed call, if any. Never
    /// written by successes, and never by the missing-key early return (the
    /// Python `except TypeError` path raises without logging either).
    pub fn last_error_draft(&self) -> Option<&LLMCallDraft> {
        self.last_error_draft.as_ref()
    }

    fn missing_key(&self) -> Option<Error> {
        if self.api_key.is_none() {
            // Mirrors the `except TypeError` branch (`anthropic.py:77-88`):
            // the SDK raises before any request when no credentials resolve.
            Some(Error::LlmUnavailable(
                "No Anthropic credentials are configured. Set RE_ANTHROPIC_API_KEY in .env, or ANTHROPIC_API_KEY in the environment."
                    .to_owned(),
            ))
        } else {
            None
        }
    }

    fn fail(
        &mut self,
        purpose: &str,
        caller: &str,
        model: &str,
        start: &Instant,
        error: Error,
        detail: &str,
    ) -> Error {
        self.last_error_draft = Some(LLMCallDraft::error(
            purpose,
            caller,
            model,
            elapsed_ms(start),
            detail,
        ));
        error
    }

    fn map_outcome(
        &mut self,
        outcome: HttpOutcome,
        purpose: &str,
        caller: &str,
        model: &str,
        start: &Instant,
    ) -> Result<serde_json::Value> {
        match outcome {
            HttpOutcome::Ok(body) => Ok(body),
            HttpOutcome::Status(code, body) => Err(self.fail(
                purpose,
                caller,
                model,
                start,
                anthropic_status(code, &body),
                &body,
            )),
            // `openai.APITimeoutError` subclasses `APIConnectionError`, and
            // the Anthropic SDK surfaces both as connection errors; both
            // adapters map them to `LLMProviderDown`.
            HttpOutcome::Timeout(detail) => Err(self.fail(
                purpose,
                caller,
                model,
                start,
                Error::LlmProviderDown(detail.clone()),
                &detail,
            )),
            HttpOutcome::Transport(detail) => Err(self.fail(
                purpose,
                caller,
                model,
                start,
                Error::LlmProviderDown(detail.clone()),
                &detail,
            )),
        }
    }

    /// Plain completion. Returns the text plus the draft the Python inserts
    /// via `LLMCallLogRepo` (`anthropic.py:49-70`).
    pub async fn complete(
        &mut self,
        messages: &[ChatMessage],
        model: Option<&str>,
        caller: &str,
        purpose: &str,
        max_tokens: Option<u32>,
    ) -> Result<(String, LLMCallDraft)> {
        if let Some(err) = self.missing_key() {
            return Err(err);
        }
        let model = model.unwrap_or(&self.default_model).to_owned();
        let start = Instant::now();
        let body = serde_json::json!({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
        });
        let outcome = self
            .http
            .post_json("/v1/messages", &body, self.call_timeout_secs)
            .await;
        let response = self.map_outcome(outcome, purpose, caller, &model, &start)?;
        let text = anthropic_text(&response).map_err(|e| {
            self.fail(
                purpose,
                caller,
                &model,
                &start,
                e,
                "unparseable response body",
            )
        })?;
        let (input_tokens, output_tokens) = anthropic_usage(&response).map_err(|e| {
            self.fail(
                purpose,
                caller,
                &model,
                &start,
                e,
                "unparseable response body",
            )
        })?;
        let duration_ms = elapsed_ms(&start);
        let draft = LLMCallDraft::ok(
            purpose,
            caller,
            &model,
            Some(input_tokens),
            Some(output_tokens),
            Some(estimate_cost(&model, input_tokens, output_tokens)),
            duration_ms,
        );
        Ok((text, draft))
    }

    /// Structured completion via `tool_use` (`anthropic.py:99-148`): declares
    /// an `extract` tool forcing `tool_choice`, returns the first
    /// `type == "tool_use"` block's `input`, else `{}`.
    pub async fn structured(
        &mut self,
        messages: &[ChatMessage],
        schema: &serde_json::Value,
        model: Option<&str>,
        caller: &str,
        purpose: &str,
        max_tokens: Option<u32>,
    ) -> Result<(serde_json::Value, LLMCallDraft)> {
        if let Some(err) = self.missing_key() {
            return Err(err);
        }
        let model = model.unwrap_or(&self.default_model).to_owned();
        let start = Instant::now();
        let body = serde_json::json!({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
            "tools": [{
                "name": "extract",
                "description": "Extract structured data from the passage.",
                "input_schema": schema,
            }],
            "tool_choice": {"type": "tool", "name": "extract"},
        });
        let outcome = self
            .http
            .post_json("/v1/messages", &body, self.call_timeout_secs)
            .await;
        let response = self.map_outcome(outcome, purpose, caller, &model, &start)?;
        let (input_tokens, output_tokens) = anthropic_usage(&response).map_err(|e| {
            self.fail(
                purpose,
                caller,
                &model,
                &start,
                e,
                "unparseable response body",
            )
        })?;
        let duration_ms = elapsed_ms(&start);
        let draft = LLMCallDraft::ok(
            purpose,
            caller,
            &model,
            Some(input_tokens),
            Some(output_tokens),
            Some(estimate_cost(&model, input_tokens, output_tokens)),
            duration_ms,
        );
        Ok((anthropic_tool_input(&response), draft))
    }
}

/// Maps an HTTP error status to the adapter taxonomy (`complete` and
/// `structured` share every branch: `anthropic.py:71-97,149-175`).
fn anthropic_status(code: u16, body: &str) -> Error {
    match code {
        // `except anthropic.AuthenticationError` (`anthropic.py:71-76`).
        401 | 403 => Error::LlmUnavailable(format!(
            "The Anthropic API rejected these credentials: {body}. Set RE_ANTHROPIC_API_KEY."
        )),
        // `except anthropic.RateLimitError` (`anthropic.py:89-91`).
        429 => Error::LlmRateLimited(body.to_owned()),
        // `except anthropic.APIConnectionError` (`anthropic.py:92-94`) is
        // covered by `HttpOutcome::Timeout/Transport`; every other `APIError`
        // (`anthropic.py:95-97`) lands here.
        _ => Error::Llm(body.to_owned()),
    }
}

/// `response.content[0].text` (`anthropic.py:55`).
fn anthropic_text(body: &serde_json::Value) -> Result<String, Error> {
    body.get("content")
        .and_then(|content| content.as_array())
        .and_then(|blocks| blocks.first())
        .and_then(|block| block.get("text"))
        .and_then(|text| text.as_str())
        .map(str::to_owned)
        .ok_or_else(|| Error::Llm("Anthropic response had no content[0].text".to_owned()))
}

/// First `type == "tool_use"` block's `input`, else `{}` (`anthropic.py:127-132`).
fn anthropic_tool_input(body: &serde_json::Value) -> serde_json::Value {
    body.get("content")
        .and_then(|content| content.as_array())
        .and_then(|blocks| {
            blocks
                .iter()
                .find(|block| block.get("type").and_then(|t| t.as_str()) == Some("tool_use"))
        })
        .and_then(|block| block.get("input"))
        .cloned()
        .unwrap_or_else(|| serde_json::json!({}))
}

/// `response.usage.input_tokens/output_tokens` (`anthropic.py:61-62,139-140`).
/// The SDK always supplies usage; a missing block is a wire violation.
fn anthropic_usage(body: &serde_json::Value) -> Result<(i64, i64), Error> {
    let usage = body
        .get("usage")
        .ok_or_else(|| Error::Llm("Anthropic response had no usage block".to_owned()))?;
    let input = usage
        .get("input_tokens")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| Error::Llm("Anthropic usage had no input_tokens".to_owned()))?;
    let output = usage
        .get("output_tokens")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| Error::Llm("Anthropic usage had no output_tokens".to_owned()))?;
    Ok((input, output))
}

/// OpenAI-compatible chat client (`POST {base}/chat/completions`).
pub struct OpenAIClient {
    http: HttpCore,
    default_model: String,
    call_timeout_secs: f64,
    last_error_draft: Option<LLMCallDraft>,
}

impl OpenAIClient {
    pub fn new(base_url: &str, api_key: Option<&str>, default_model: Option<&str>) -> Self {
        // `api_key or "unused"` (`openai_compatible.py:28`); the bearer is
        // applied per request by `HttpCore`
        // (`.venv/.../openai/_client.py:316-320`).
        let key = api_key.unwrap_or(OPENAI_UNUSED_KEY);
        let http = HttpCore::build(base_url, Some(key), &[], CONNECT_TIMEOUT, REQUEST_TIMEOUT);
        Self {
            http,
            default_model: default_model.unwrap_or(OPENAI_DEFAULT_MODEL).to_owned(),
            call_timeout_secs: DEFAULT_CALL_TIMEOUT_SECS,
            last_error_draft: None,
        }
    }

    /// Override the per-call timeout (tests; default is the SDK 600 s).
    pub fn set_call_timeout(&mut self, secs: f64) {
        self.call_timeout_secs = secs;
    }

    /// The most recent failed call's draft (the OpenAI adapter has no
    /// `_log_error` on success paths either; its `except` branches raise
    /// without inserting, so only transport/status/parse failures land here).
    pub fn last_error_draft(&self) -> Option<&LLMCallDraft> {
        self.last_error_draft.as_ref()
    }

    fn fail(
        &mut self,
        purpose: &str,
        caller: &str,
        model: &str,
        start: &Instant,
        error: Error,
        detail: &str,
    ) -> Error {
        self.last_error_draft = Some(LLMCallDraft::error(
            purpose,
            caller,
            model,
            elapsed_ms(start),
            detail,
        ));
        error
    }

    fn map_outcome(
        &mut self,
        outcome: HttpOutcome,
        purpose: &str,
        caller: &str,
        model: &str,
        start: &Instant,
    ) -> Result<serde_json::Value> {
        match outcome {
            HttpOutcome::Ok(body) => Ok(body),
            HttpOutcome::Status(code, body) => Err(self.fail(
                purpose,
                caller,
                model,
                start,
                openai_status(code, &body),
                &body,
            )),
            HttpOutcome::Timeout(detail) => Err(self.fail(
                purpose,
                caller,
                model,
                start,
                Error::LlmProviderDown(detail.clone()),
                &detail,
            )),
            HttpOutcome::Transport(detail) => Err(self.fail(
                purpose,
                caller,
                model,
                start,
                Error::LlmProviderDown(detail.clone()),
                &detail,
            )),
        }
    }

    /// Plain completion (`openai_compatible.py:32-68`): text plus the draft.
    /// No `cost_estimate` — the Python never sets one here.
    pub async fn complete(
        &mut self,
        messages: &[ChatMessage],
        model: Option<&str>,
        caller: &str,
        purpose: &str,
        max_tokens: Option<u32>,
    ) -> Result<(String, LLMCallDraft)> {
        let model = model.unwrap_or(&self.default_model).to_owned();
        let start = Instant::now();
        let body = serde_json::json!({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
        });
        let outcome = self
            .http
            .post_json("/chat/completions", &body, self.call_timeout_secs)
            .await;
        let response = self.map_outcome(outcome, purpose, caller, &model, &start)?;
        let message = openai_message(&response).map_err(|e| {
            self.fail(
                purpose,
                caller,
                &model,
                &start,
                e,
                "unparseable response body",
            )
        })?;
        // `response.choices[0].message.content or ""`
        // (`openai_compatible.py:49`).
        let text = message
            .get("content")
            .and_then(|content| content.as_str())
            .unwrap_or("")
            .to_owned();
        let (input_tokens, output_tokens) = openai_usage(&response);
        let draft = LLMCallDraft::ok(
            purpose,
            caller,
            &model,
            input_tokens,
            output_tokens,
            None,
            elapsed_ms(&start),
        );
        Ok((text, draft))
    }

    /// Structured completion via function-calling (`openai_compatible.py:70-119`).
    pub async fn structured(
        &mut self,
        messages: &[ChatMessage],
        schema: &serde_json::Value,
        model: Option<&str>,
        caller: &str,
        purpose: &str,
        max_tokens: Option<u32>,
    ) -> Result<(serde_json::Value, LLMCallDraft)> {
        let model = model.unwrap_or(&self.default_model).to_owned();
        let start = Instant::now();
        let body = serde_json::json!({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens.unwrap_or(DEFAULT_MAX_TOKENS),
            "tools": [{
                "type": "function",
                "function": {
                    "name": "extract",
                    "description": "Extract structured data",
                    "parameters": schema,
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "extract"}},
        });
        let outcome = self
            .http
            .post_json("/chat/completions", &body, self.call_timeout_secs)
            .await;
        let response = self.map_outcome(outcome, purpose, caller, &model, &start)?;
        let message = openai_message(&response).map_err(|e| {
            self.fail(
                purpose,
                caller,
                &model,
                &start,
                e,
                "unparseable response body",
            )
        })?;
        // `json.loads(tool_call.function.arguments)`
        // (`openai_compatible.py:99-100`); the Python lets the `json` error
        // propagate raw, we wrap it as `Llm`.
        let arguments = message
            .get("tool_calls")
            .and_then(|calls| calls.as_array())
            .and_then(|calls| calls.first())
            .and_then(|call| call.get("function"))
            .and_then(|function| function.get("arguments"))
            .and_then(|arguments| arguments.as_str())
            .ok_or_else(|| {
                Error::Llm("OpenAI response had no tool_calls[0].function.arguments".to_owned())
            })
            .map_err(|e| {
                self.fail(
                    purpose,
                    caller,
                    &model,
                    &start,
                    e,
                    "unparseable response body",
                )
            })?;
        let parsed: serde_json::Value = serde_json::from_str(arguments).map_err(|e| {
            self.fail(
                purpose,
                caller,
                &model,
                &start,
                Error::Llm(format!("OpenAI tool arguments were not JSON: {e}")),
                "unparseable response body",
            )
        })?;
        let (input_tokens, output_tokens) = openai_usage(&response);
        let draft = LLMCallDraft::ok(
            purpose,
            caller,
            &model,
            input_tokens,
            output_tokens,
            None,
            elapsed_ms(&start),
        );
        Ok((parsed, draft))
    }
}

/// Status taxonomy for the OpenAI-compatible path
/// (`openai_compatible.py:63-68,114-119`): `RateLimitError -> LLMRateLimited`,
/// `APIConnectionError -> LLMProviderDown` (covered by
/// `Timeout`/`Transport`), any other `APIError -> LLMError`. Auth rejection
/// has no dedicated branch in the Python, but a 401/403 is unactionable
/// without new credentials, so it maps to `LlmUnavailable` like the
/// Anthropic side.
fn openai_status(code: u16, body: &str) -> Error {
    match code {
        401 | 403 => Error::LlmUnavailable(format!(
            "The OpenAI-compatible API rejected these credentials: {body}."
        )),
        429 => Error::LlmRateLimited(body.to_owned()),
        _ => Error::Llm(body.to_owned()),
    }
}

/// `response.choices[0].message` (`openai_compatible.py:49,99`).
fn openai_message(body: &serde_json::Value) -> Result<serde_json::Value, Error> {
    body.get("choices")
        .and_then(|choices| choices.as_array())
        .and_then(|choices| choices.first())
        .and_then(|choice| choice.get("message"))
        .cloned()
        .ok_or_else(|| Error::Llm("OpenAI response had no choices[0].message".to_owned()))
}

/// `usage.prompt_tokens/completion_tokens if usage else None`
/// (`openai_compatible.py:56-57,107-108`): each side is `None` when missing.
fn openai_usage(body: &serde_json::Value) -> (Option<i64>, Option<i64>) {
    let usage = body.get("usage");
    let input = usage
        .and_then(|usage| usage.get("prompt_tokens"))
        .and_then(|v| v.as_i64());
    let output = usage
        .and_then(|usage| usage.get("completion_tokens"))
        .and_then(|v| v.as_i64());
    (input, output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc;
    use std::thread;

    struct CapturedRequest {
        method: String,
        path: String,
        headers: Vec<(String, String)>,
        body: serde_json::Value,
    }

    fn reason(status: u16) -> &'static str {
        match status {
            200 => "OK",
            401 => "Unauthorized",
            403 => "Forbidden",
            429 => "Too Many Requests",
            500 => "Internal Server Error",
            503 => "Service Unavailable",
            _ => "Error",
        }
    }

    fn read_request(stream: &std::net::TcpStream) -> CapturedRequest {
        let mut reader = BufReader::new(stream);
        let mut request_line = String::new();
        reader.read_line(&mut request_line).expect("request line");
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or("").to_owned();
        let path = parts.next().unwrap_or("").to_owned();
        let mut headers = Vec::new();
        let mut content_length = 0usize;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).expect("header line");
            let line = line.trim_end().to_owned();
            if line.is_empty() {
                break;
            }
            if let Some((name, value)) = line.split_once(':') {
                if name.trim().eq_ignore_ascii_case("content-length") {
                    content_length = value.trim().parse().unwrap_or(0);
                }
                headers.push((name.trim().to_owned(), value.trim().to_owned()));
            }
        }
        let mut raw = vec![0u8; content_length];
        reader.read_exact(&mut raw).expect("request body");
        let body = serde_json::from_slice(&raw).unwrap_or(serde_json::Value::Null);
        CapturedRequest {
            method,
            path,
            headers,
            body,
        }
    }

    fn respond(stream: &std::net::TcpStream, status: u16, body: &str) {
        let response = format!(
            "HTTP/1.1 {status} {}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
            reason(status),
            body.len()
        );
        let mut stream = stream.try_clone().expect("clone stream");
        let _ = stream.write_all(response.as_bytes());
    }

    /// Serves exactly one connection on loopback, then exits. Returns the
    /// base URL, the captured request, and the server thread handle.
    fn serve_once(
        status: u16,
        response_body: &str,
    ) -> (
        String,
        mpsc::Receiver<CapturedRequest>,
        thread::JoinHandle<()>,
    ) {
        serve_once_delayed(status, response_body, Duration::ZERO)
    }

    fn serve_once_delayed(
        status: u16,
        response_body: &str,
        delay: Duration,
    ) -> (
        String,
        mpsc::Receiver<CapturedRequest>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        let base_url = format!("http://{}", listener.local_addr().expect("addr"));
        let (tx, rx) = mpsc::channel();
        let owned = response_body.to_owned();
        let handle = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept");
            let captured = read_request(&stream);
            let _ = tx.send(captured);
            if !delay.is_zero() {
                thread::sleep(delay);
            }
            respond(&stream, status, &owned);
        });
        (base_url, rx, handle)
    }

    fn header<'a>(captured: &'a CapturedRequest, name: &str) -> Option<&'a str> {
        captured
            .headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    fn messages() -> Vec<ChatMessage> {
        vec![ChatMessage::new("user", "Summarize this passage.")]
    }

    fn schema() -> serde_json::Value {
        serde_json::json!({"type": "object", "properties": {"claim": {"type": "string"}}})
    }

    #[tokio::test]
    async fn anthropic_complete_ok() {
        let (base, rx, server) = serve_once(
            200,
            r#"{"content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 10, "output_tokens": 5}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("test-key"), None, &base);
        let (text, draft) = client
            .complete(&messages(), None, "test", "general", None)
            .await
            .expect("complete");
        assert_eq!(text, "hello");
        assert_eq!(draft.purpose, "general");
        assert_eq!(draft.caller, "test");
        assert_eq!(draft.model, "claude-sonnet-4-5-20250929");
        assert_eq!(draft.input_tokens, Some(10));
        assert_eq!(draft.output_tokens, Some(5));
        assert_eq!(
            draft.cost_estimate,
            Some(estimate_cost(draft.model.as_str(), 10, 5))
        );
        assert_eq!(draft.status, "ok");
        assert_eq!(draft.error, None);
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);

        let captured = rx.recv_timeout(Duration::from_secs(5)).expect("request");
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/messages");
        assert_eq!(header(&captured, "x-api-key"), Some("test-key"));
        assert_eq!(header(&captured, "anthropic-version"), Some("2023-06-01"));
        assert_eq!(captured.body["model"], "claude-sonnet-4-5-20250929");
        assert_eq!(captured.body["max_tokens"], 4096);
        assert_eq!(captured.body["messages"][0]["role"], "user");
        assert!(captured.body.get("tools").is_none());
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_model_and_max_tokens_override() {
        let (base, rx, server) = serve_once(
            200,
            r#"{"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 1, "output_tokens": 1}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), Some("other-model"), &base);
        let (_, draft) = client
            .complete(&messages(), Some("explicit-model"), "c", "p", Some(128))
            .await
            .expect("complete");
        assert_eq!(draft.model, "explicit-model");
        let captured = rx.recv_timeout(Duration::from_secs(5)).expect("request");
        assert_eq!(captured.body["model"], "explicit-model");
        assert_eq!(captured.body["max_tokens"], 128);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_structured_first_tool_use_wins() {
        let (base, rx, server) = serve_once(
            200,
            r#"{"content": [
                {"type": "text", "text": "thinking"},
                {"type": "tool_use", "id": "a", "name": "extract", "input": {"claim": "first"}},
                {"type": "tool_use", "id": "b", "name": "extract", "input": {"claim": "second"}}
            ], "usage": {"input_tokens": 20, "output_tokens": 8}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let (parsed, draft) = client
            .structured(&messages(), &schema(), None, "test", "extraction", None)
            .await
            .expect("structured");
        assert_eq!(parsed, serde_json::json!({"claim": "first"}));
        assert_eq!(draft.purpose, "extraction");
        assert_eq!(draft.input_tokens, Some(20));
        assert_eq!(draft.output_tokens, Some(8));

        let captured = rx.recv_timeout(Duration::from_secs(5)).expect("request");
        assert_eq!(captured.path, "/v1/messages");
        assert_eq!(
            captured.body["tools"],
            serde_json::json!([{
                "name": "extract",
                "description": "Extract structured data from the passage.",
                "input_schema": schema(),
            }])
        );
        assert_eq!(
            captured.body["tool_choice"],
            serde_json::json!({"type": "tool", "name": "extract"})
        );
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_structured_no_tool_use_is_empty_object() {
        let (base, _rx, server) = serve_once(
            200,
            r#"{"content": [{"type": "text", "text": "no tools here"}], "usage": {"input_tokens": 4, "output_tokens": 2}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let (parsed, _) = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect("structured");
        assert_eq!(parsed, serde_json::json!({}));
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_missing_usage_is_error() {
        let (base, _rx, server) =
            serve_once(200, r#"{"content": [{"type": "text", "text": "hello"}]}"#);
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("missing usage");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::Llm(String::new()))
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);
        server.join().expect("server");
    }
    #[tokio::test]
    async fn anthropic_usage_missing_input_tokens_is_error() {
        // A half-present usage block: the SDK would fail `Usage` validation
        // (`input_tokens: int` is required) and escape raw; the typed
        // boundary answers `Llm` instead — both are errors, ours is matchable.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"content": [{"type": "text", "text": "hi"}], "usage": {"output_tokens": 2}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("missing input_tokens");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::Llm(String::new()))
        );
        assert!(client.last_error_draft().is_some());
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_usage_missing_output_tokens_is_error() {
        let (base, _rx, server) = serve_once(
            200,
            r#"{"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 4}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("missing output_tokens");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::Llm(String::new()))
        );
        assert!(client.last_error_draft().is_some());
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_401_maps_unavailable_exact() {
        let (base, _rx, server) = serve_once(401, r#"{"error": "invalid x-api-key"}"#);
        let mut client = AnthropicClient::with_base_url(Some("bad"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("401");
        assert_eq!(
            err,
            Error::LlmUnavailable(
                r#"The Anthropic API rejected these credentials: {"error": "invalid x-api-key"}. Set RE_ANTHROPIC_API_KEY."#
                    .to_owned()
            )
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(
            draft.error.as_deref(),
            Some(r#"{"error": "invalid x-api-key"}"#)
        );
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.cost_estimate, None);
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_403_maps_unavailable() {
        let (base, _rx, server) = serve_once(403, "forbidden");
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("403");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::LlmUnavailable(String::new()))
        );
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_429_maps_rate_limited() {
        let (base, _rx, server) = serve_once(429, "slow down");
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("429");
        assert_eq!(err, Error::LlmRateLimited("slow down".to_owned()));
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_503_maps_llm() {
        let (base, _rx, server) = serve_once(503, "overloaded");
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("503");
        assert_eq!(err, Error::Llm("overloaded".to_owned()));
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_timeout_maps_provider_down() {
        let (base, _rx, server) =
            serve_once_delayed(200, r#"{"content": []}"#, Duration::from_secs(2));
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        client.set_call_timeout(0.3);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("timeout");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::LlmProviderDown(String::new()))
        );
        assert!(client.last_error_draft().is_some());
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_missing_key_never_sends() {
        // No stub: any request attempt would fail to connect, so reaching the
        // missing-key error proves nothing was sent.
        let mut client = AnthropicClient::with_base_url(None, None, "http://127.0.0.1:9");
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("missing key");
        assert_eq!(
            err,
            Error::LlmUnavailable(
                "No Anthropic credentials are configured. Set RE_ANTHROPIC_API_KEY in .env, or ANTHROPIC_API_KEY in the environment."
                    .to_owned()
            )
        );
        assert_eq!(client.last_error_draft(), None);
    }

    #[tokio::test]
    async fn openai_complete_ok_with_unused_key() {
        let (base, rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}"#,
        );
        let mut client = OpenAIClient::new(&base, None, None);
        let (text, draft) = client
            .complete(&messages(), None, "test", "general", None)
            .await
            .expect("complete");
        assert_eq!(text, "hi");
        assert_eq!(draft.model, "gpt-4o");
        assert_eq!(draft.input_tokens, Some(7));
        assert_eq!(draft.output_tokens, Some(3));
        // The Python never sets cost_estimate on this path.
        assert_eq!(draft.cost_estimate, None);
        assert_eq!(draft.status, "ok");

        let captured = rx.recv_timeout(Duration::from_secs(5)).expect("request");
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/chat/completions");
        assert_eq!(header(&captured, "authorization"), Some("Bearer unused"));
        assert_eq!(header(&captured, "x-api-key"), None);
        assert_eq!(captured.body["model"], "gpt-4o");
        assert_eq!(captured.body["max_tokens"], 4096);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_complete_null_content_is_empty() {
        let (base, _rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "content": null}}], "usage": {"prompt_tokens": 1, "completion_tokens": 0}}"#,
        );
        let mut client = OpenAIClient::new(&base, Some("sk-live"), Some("gpt-4o-mini"));
        let (text, draft) = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect("complete");
        assert_eq!(text, "");
        assert_eq!(draft.model, "gpt-4o-mini");
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_structured_parses_tool_arguments() {
        let (base, rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "extract", "arguments": "{\"claim\": \"parsed\"}"}}]}}], "usage": {"prompt_tokens": 12, "completion_tokens": 6}}"#,
        );
        let mut client = OpenAIClient::new(&base, Some("sk-live"), None);
        let (parsed, draft) = client
            .structured(&messages(), &schema(), None, "test", "extraction", None)
            .await
            .expect("structured");
        assert_eq!(parsed, serde_json::json!({"claim": "parsed"}));
        assert_eq!(draft.input_tokens, Some(12));
        assert_eq!(draft.output_tokens, Some(6));

        let captured = rx.recv_timeout(Duration::from_secs(5)).expect("request");
        assert_eq!(header(&captured, "authorization"), Some("Bearer sk-live"));
        assert_eq!(
            captured.body["tools"],
            serde_json::json!([{
                "type": "function",
                "function": {
                    "name": "extract",
                    "description": "Extract structured data",
                    "parameters": schema(),
                },
            }])
        );
        assert_eq!(
            captured.body["tool_choice"],
            serde_json::json!({"type": "function", "function": {"name": "extract"}})
        );
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_missing_usage_gives_none_tokens() {
        let (base, _rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "content": "hi"}}]}"#,
        );
        let mut client = OpenAIClient::new(&base, None, None);
        let (text, draft) = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect("complete");
        assert_eq!(text, "hi");
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_401_maps_unavailable() {
        let (base, _rx, server) = serve_once(401, "bad credentials");
        let mut client = OpenAIClient::new(&base, Some("sk-bad"), None);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("401");
        assert_eq!(
            err,
            Error::LlmUnavailable(
                "The OpenAI-compatible API rejected these credentials: bad credentials.".to_owned()
            )
        );
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_429_maps_rate_limited() {
        let (base, _rx, server) = serve_once(429, "quota");
        let mut client = OpenAIClient::new(&base, None, None);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("429");
        assert_eq!(err, Error::LlmRateLimited("quota".to_owned()));
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_503_maps_llm() {
        let (base, _rx, server) = serve_once(503, "unavailable");
        let mut client = OpenAIClient::new(&base, None, None);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("503");
        assert_eq!(err, Error::Llm("unavailable".to_owned()));
        server.join().expect("server");
    }

    #[test]
    fn cost_table_boundaries() {
        // (1000 * in + 500 * out) / 1e6 per model, plus the default-model
        // fallback (`costs.get(model, (3.0, 15.0))`, `anthropic.py:200`).
        assert_eq!(
            estimate_cost("claude-sonnet-4-5-20250929", 1000, 500),
            (1000.0f64 * 3.0 + 500.0 * 15.0) / 1_000_000.0
        );
        assert_eq!(
            estimate_cost("claude-haiku-4-5-20251001", 1000, 500),
            (1000.0f64 * 0.80 + 500.0 * 4.0) / 1_000_000.0
        );
        assert_eq!(
            estimate_cost("claude-opus-4-6", 1000, 500),
            (1000.0f64 * 15.0 + 500.0 * 75.0) / 1_000_000.0
        );
        assert_eq!(
            estimate_cost("gpt-4o", 1000, 500),
            estimate_cost("claude-sonnet-4-5-20250929", 1000, 500)
        );
        assert_eq!(estimate_cost("anything-else", 0, 0), 0.0);
    }

    #[test]
    fn duration_truncates_like_python_int() {
        // `int()` truncates; a rounding implementation would fail these.
        assert_eq!(trunc_ms(0.0), 0);
        assert_eq!(trunc_ms(0.001_9), 1);
        assert_eq!(trunc_ms(1.234_567_8), 1234);
        assert_eq!(trunc_ms(60.0), 60_000);
    }
    fn closed_loopback_port() -> u16 {
        // A port nothing is listening on: bind, record, release. The next
        // connect gets RST (`HttpOutcome::Transport`), never an HTTP response.
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        let port = listener.local_addr().expect("addr").port();
        drop(listener);
        port
    }

    #[test]
    fn anthropic_new_uses_pinned_defaults() {
        // `AnthropicClient::new` pins the SDK base URL plus the adapter
        // default model (`anthropic.py:31`); construction sends nothing, so
        // the live default URL is safe to build here.
        let client = AnthropicClient::new(Some("k"), None);
        assert_eq!(client.default_model, ANTHROPIC_DEFAULT_MODEL);
        assert_eq!(client.last_error_draft(), None);
        let explicit = AnthropicClient::new(None, Some("other-model"));
        assert_eq!(explicit.default_model, "other-model");
        assert_eq!(explicit.last_error_draft(), None);
    }

    #[tokio::test]
    async fn anthropic_structured_missing_key_never_sends() {
        // Structured-path `except TypeError` (the `anthropic.py:149ff`
        // handlers never run): the SDK raises before any request and the
        // Python logs nothing, hence no draft here either.
        let mut client = AnthropicClient::with_base_url(None, None, "http://127.0.0.1:9");
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("missing key");
        assert_eq!(
            err,
            Error::LlmUnavailable(
                "No Anthropic credentials are configured. Set RE_ANTHROPIC_API_KEY in .env, or ANTHROPIC_API_KEY in the environment."
                    .to_owned()
            )
        );
        assert_eq!(client.last_error_draft(), None);
    }

    #[tokio::test]
    async fn anthropic_transport_maps_provider_down() {
        // `except anthropic.APIConnectionError -> LLMProviderDown`
        // (`anthropic.py:92-94`) via `HttpOutcome::Transport`: refused
        // loopback connection, never an HTTP response.
        let base = format!("http://127.0.0.1:{}", closed_loopback_port());
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("transport");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::LlmProviderDown(String::new()))
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.purpose, "p");
        assert_eq!(draft.caller, "c");
        assert_eq!(draft.model, ANTHROPIC_DEFAULT_MODEL);
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        assert_eq!(draft.cost_estimate, None);
        assert_eq!(draft.status, "error");
        assert!(draft.error.is_some());
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);
    }

    #[tokio::test]
    async fn anthropic_complete_empty_content_is_error() {
        // `response.content[0].text` (`anthropic.py:55`) on an empty block
        // list: usage arrived but there is no text, so the `_log_error`
        // draft (`anthropic.py:177-190`) carries no tokens.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"content": [], "usage": {"input_tokens": 3, "output_tokens": 1}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("empty content");
        assert_eq!(
            err,
            Error::Llm("Anthropic response had no content[0].text".to_owned())
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("unparseable response body"));
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        assert_eq!(draft.cost_estimate, None);
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_structured_missing_usage_is_error() {
        // Structured-path `response.usage` (`anthropic.py:139-140`): the SDK
        // always supplies usage, so a missing block is the same wire
        // violation as on `complete`, logged with no tokens.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"content": [{"type": "tool_use", "input": {"claim": "x"}}]}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("missing usage");
        assert_eq!(
            err,
            Error::Llm("Anthropic response had no usage block".to_owned())
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("unparseable response body"));
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_structured_500_maps_llm() {
        // Any other `APIError -> LLMError` (`anthropic.py:95-97`) on the
        // structured path; the 500 also pins the `reason` helper's 500 arm.
        let (base, _rx, server) = serve_once(500, "boom");
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("500");
        assert_eq!(err, Error::Llm("boom".to_owned()));
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("boom"));
        assert_eq!(draft.input_tokens, None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn anthropic_structured_ok_draft_full() {
        // Structured ok-draft mirrors `complete`'s (`anthropic.py:134-147`):
        // tokens, sonnet cost, `status: "ok"`, no error text, and success
        // never writes an error draft.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"content": [{"type": "tool_use", "input": {"claim": "x"}}], "usage": {"input_tokens": 6, "output_tokens": 2}}"#,
        );
        let mut client = AnthropicClient::with_base_url(Some("k"), None, &base);
        let (parsed, draft) = client
            .structured(&messages(), &schema(), None, "test", "extraction", None)
            .await
            .expect("structured");
        assert_eq!(parsed, serde_json::json!({"claim": "x"}));
        assert_eq!(draft.model, ANTHROPIC_DEFAULT_MODEL);
        assert_eq!(draft.purpose, "extraction");
        assert_eq!(draft.caller, "test");
        assert_eq!(draft.input_tokens, Some(6));
        assert_eq!(draft.output_tokens, Some(2));
        assert_eq!(
            draft.cost_estimate,
            Some(estimate_cost(ANTHROPIC_DEFAULT_MODEL, 6, 2))
        );
        assert_eq!(draft.status, "ok");
        assert_eq!(draft.error, None);
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);
        assert_eq!(client.last_error_draft(), None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_timeout_maps_provider_down() {
        // `except openai.APIConnectionError -> LLMProviderDown`
        // (`openai_compatible.py:65-66,116-117`) via `HttpOutcome::Timeout`;
        // also pins `set_call_timeout`.
        let (base, _rx, server) =
            serve_once_delayed(200, r#"{"choices": []}"#, Duration::from_secs(2));
        let mut client = OpenAIClient::new(&base, Some("sk-live"), None);
        client.set_call_timeout(0.3);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("timeout");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::LlmProviderDown(String::new()))
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert!(draft.error.as_deref().unwrap_or("").starts_with("Timeout"));
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        assert_eq!(draft.model, "gpt-4o");
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_transport_maps_provider_down() {
        // Same `APIConnectionError` branch via `HttpOutcome::Transport`:
        // refused loopback connection, never an HTTP response.
        let base = format!("http://127.0.0.1:{}", closed_loopback_port());
        let mut client = OpenAIClient::new(&base, Some("sk-live"), None);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("transport");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::LlmProviderDown(String::new()))
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert!(draft.error.is_some());
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        assert_eq!(draft.model, "gpt-4o");
    }

    #[tokio::test]
    async fn openai_complete_empty_choices_is_error() {
        // `response.choices[0].message` (`openai_compatible.py:49`) with no
        // choices at all: no tokens arrived, so the draft stays token-less.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}"#,
        );
        let mut client = OpenAIClient::new(&base, None, None);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("empty choices");
        assert_eq!(
            err,
            Error::Llm("OpenAI response had no choices[0].message".to_owned())
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("unparseable response body"));
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_structured_empty_choices_is_error() {
        // Same `choices[0]` edge (`openai_compatible.py:99`) on the
        // structured path.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}"#,
        );
        let mut client = OpenAIClient::new(&base, None, None);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("empty choices");
        assert_eq!(
            err,
            Error::Llm("OpenAI response had no choices[0].message".to_owned())
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("unparseable response body"));
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_structured_missing_tool_call_is_error() {
        // `response.choices[0].message.tool_calls[0]`
        // (`openai_compatible.py:99`): the Python lets the non-SDK
        // `IndexError`/`TypeError` propagate raw; without an SDK error to
        // map, we wrap it as `Llm` and keep the token-less error draft.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}"#,
        );
        let mut client = OpenAIClient::new(&base, None, None);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("missing tool call");
        assert_eq!(
            err,
            Error::Llm("OpenAI response had no tool_calls[0].function.arguments".to_owned())
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("unparseable response body"));
        assert_eq!(draft.input_tokens, None);
        assert_eq!(draft.output_tokens, None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_structured_non_json_arguments_is_error() {
        // `json.loads(tool_call.function.arguments)`
        // (`openai_compatible.py:100`): the Python lets the `json` error
        // propagate raw; we wrap it as `Llm` the same way.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "extract", "arguments": "not json {"}}]}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}"#,
        );
        let mut client = OpenAIClient::new(&base, None, None);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("non-JSON arguments");
        assert!(
            matches!(&err, Error::Llm(detail) if detail.starts_with("OpenAI tool arguments were not JSON")),
            "got {err:?}"
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("unparseable response body"));
        assert_eq!(draft.input_tokens, None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_structured_ok_draft_full() {
        // Structured ok-draft (`openai_compatible.py:102-112`): tokens when
        // present, never a cost estimate, `status: "ok"`, no error text.
        let (base, _rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "extract", "arguments": "{\"claim\": \"parsed\"}"}}]}}], "usage": {"prompt_tokens": 12, "completion_tokens": 6}}"#,
        );
        let mut client = OpenAIClient::new(&base, Some("sk-live"), None);
        let (parsed, draft) = client
            .structured(&messages(), &schema(), None, "test", "extraction", None)
            .await
            .expect("structured");
        assert_eq!(parsed, serde_json::json!({"claim": "parsed"}));
        assert_eq!(draft.model, "gpt-4o");
        assert_eq!(draft.purpose, "extraction");
        assert_eq!(draft.caller, "test");
        assert_eq!(draft.input_tokens, Some(12));
        assert_eq!(draft.output_tokens, Some(6));
        assert_eq!(draft.cost_estimate, None);
        assert_eq!(draft.status, "ok");
        assert_eq!(draft.error, None);
        assert!(draft.duration_ms.unwrap_or(-1) >= 0);
        assert_eq!(client.last_error_draft(), None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_403_maps_unavailable() {
        // Auth rejection has no dedicated branch in the Python, but a
        // 401/403 is unactionable without new credentials, so it maps to
        // `LlmUnavailable` like the Anthropic side; asserted on the
        // structured path with its error draft.
        let (base, _rx, server) = serve_once(403, "forbidden");
        let mut client = OpenAIClient::new(&base, Some("sk-bad"), None);
        let err = client
            .structured(&messages(), &schema(), None, "c", "p", None)
            .await
            .expect_err("403");
        assert_eq!(
            err,
            Error::LlmUnavailable(
                "The OpenAI-compatible API rejected these credentials: forbidden.".to_owned()
            )
        );
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("forbidden"));
        assert_eq!(draft.input_tokens, None);
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_complete_unexpected_status_maps_llm() {
        // Any other `APIError -> LLMError` (`openai_compatible.py:67-68`);
        // the 418 also pins the `reason` helper's wildcard arm.
        let (base, _rx, server) = serve_once(418, "teapot");
        let mut client = OpenAIClient::new(&base, None, None);
        let err = client
            .complete(&messages(), None, "c", "p", None)
            .await
            .expect_err("418");
        assert_eq!(err, Error::Llm("teapot".to_owned()));
        let draft = client.last_error_draft().expect("draft recorded");
        assert_eq!(draft.status, "error");
        assert_eq!(draft.error.as_deref(), Some("teapot"));
        server.join().expect("server");
    }

    #[tokio::test]
    async fn openai_model_and_max_tokens_override() {
        // `kwargs.get("max_tokens", 4096)` (`openai_compatible.py:46`) plus
        // the `model or self._default_model` fallback, on this adapter; a
        // fresh success writes no error draft.
        let (base, rx, server) = serve_once(
            200,
            r#"{"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}"#,
        );
        let mut client = OpenAIClient::new(&base, Some("sk-live"), Some("gpt-4o-mini"));
        assert_eq!(client.last_error_draft(), None);
        let (text, draft) = client
            .complete(&messages(), Some("explicit-model"), "c", "p", Some(77))
            .await
            .expect("complete");
        assert_eq!(text, "hi");
        assert_eq!(draft.model, "explicit-model");
        assert_eq!(client.last_error_draft(), None);
        let captured = rx.recv_timeout(Duration::from_secs(5)).expect("request");
        assert_eq!(captured.body["model"], "explicit-model");
        assert_eq!(captured.body["max_tokens"], 77);
        server.join().expect("server");
    }

    #[test]
    fn read_request_ignores_header_without_colon() {
        // False edge of the `split_once(':')` guard in the loopback
        // `read_request` helper: real clients always send well-formed
        // headers, so drive it with a hand-written request instead.
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        let addr = listener.local_addr().expect("addr");
        let writer = thread::spawn(move || {
            let mut stream = std::net::TcpStream::connect(addr).expect("connect");
            stream
                .write_all(
                    b"POST /v1/messages HTTP/1.1\r\nnot-a-header-line\r\ncontent-length: 2\r\n\r\n{}",
                )
                .expect("write");
        });
        let (server_stream, _) = listener.accept().expect("accept");
        let captured = read_request(&server_stream);
        writer.join().expect("writer");
        assert_eq!(captured.method, "POST");
        assert_eq!(captured.path, "/v1/messages");
        assert_eq!(captured.headers.len(), 1);
        assert_eq!(captured.body, serde_json::json!({}));
    }
}
