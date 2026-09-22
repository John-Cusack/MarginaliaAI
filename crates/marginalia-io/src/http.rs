//! Shared HTTP core for the Phase 6 JSON clients.
//!
//! Every JSON-speaking client in this crate (reranker, embedding, LLM) routes
//! its requests through the single [`HttpCore::post_json`] /
//! [`HttpCore::get_json`] pair below. That monomorphism is load-bearing for
//! coverage (one instantiation to pin) and for uniformity: timeout,
//! redirect, auth, and error-classification behaviour is decided once, here.
//! Do not add typed per-client variants; route new clients through these two.
//!
//! [`HttpAdapter`] separately ports the raw-bytes `HttpPort`
//! (`adapters/http/httpx_adapter.py`); it shares the client construction but
//! not the JSON round-trip, because it serves callers that want bytes.

use std::time::Duration;

use serde_json::Value;

use crate::errors::{Error, Result};

/// Default timeout (seconds) of [`HttpAdapter`], mirroring the
/// `timeout: float = 30.0` default on `HttpxAdapter.__init__`.
pub const HTTP_ADAPTER_DEFAULT_TIMEOUT_SECS: f64 = 30.0;

/// Outcome of one JSON round-trip. Non-2xx answers surface as [`HttpOutcome::Status`]
/// with the body verbatim (truncation, if any, is the caller's job — the
/// reranker keeps the first 200 chars, like the Python slice); anything that
/// never produced a response is [`HttpOutcome::Timeout`] or
/// [`HttpOutcome::Transport`].
#[derive(Debug)]
pub(crate) enum HttpOutcome {
    Ok(Value),
    Status(u16, String),
    Timeout(String),
    Transport(String),
}

/// Shared `reqwest` client plus the address and credentials every request needs.
///
/// Client-shape parity is with `HttpxAdapter`
/// (`adapters/http/httpx_adapter.py:12`):
/// A scalar httpx
/// timeout fills all four phases (connect/read/write/pool) with `X`
/// (`httpx/_config.py:127-130`); reqwest pins the equivalents with
/// `connect_timeout` (`reqwest-0.13.4/src/async_impl/client.rs:1469`) plus the
/// total-deadline `timeout` (`async_impl/client.rs:1444`). Redirects are
/// followed (`httpx_adapter.py` passes `follow_redirects=True`); the bound is
/// httpx's `DEFAULT_MAX_REDIRECTS = 20` (`httpx/_config.py:248`) rather than
/// reqwest's default of 10 (`reqwest-0.13.4/src/redirect.rs:160-165`).
pub(crate) struct HttpCore {
    pub(crate) client: reqwest::Client,
    pub(crate) base_url: String,
    pub(crate) bearer: Option<String>,
    pub(crate) extra_headers: Vec<(String, String)>,
}

impl HttpCore {
    pub(crate) fn build(
        base_url: &str,
        bearer: Option<&str>,
        extra_headers: &[(&str, &str)],
        connect_timeout: Duration,
        request_timeout: Duration,
    ) -> Self {
        Self {
            client: build_client(connect_timeout, request_timeout),
            // The Python clients store `base_url.rstrip("/")` and post
            // absolute paths like `/rerank`; keep that shape here.
            base_url: base_url.trim_end_matches('/').to_owned(),
            bearer: bearer.map(str::to_owned),
            // Anthropic-style `x-api-key` / `anthropic-version` headers ride
            // here; `Authorization: Bearer` stays in `bearer`.
            extra_headers: extra_headers
                .iter()
                .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
                .collect(),
        }
    }

    /// The one POST entry point. `timeout_secs` overrides the client default
    /// for this request (the reranker handshake passes its 10 s budget this
    /// way, as `RemoteReranker.health` passes `timeout=10.0`).
    pub(crate) async fn post_json(
        &self,
        path: &str,
        body: &Value,
        timeout_secs: f64,
    ) -> HttpOutcome {
        let request = self.apply_auth(self.client.post(self.url(path)).json(body));
        self.round_trip(request, timeout_secs).await
    }

    /// The one GET entry point.
    pub(crate) async fn get_json(&self, path: &str, timeout_secs: f64) -> HttpOutcome {
        let request = self.apply_auth(self.client.get(self.url(path)));
        self.round_trip(request, timeout_secs).await
    }

    fn url(&self, path: &str) -> String {
        if path.starts_with('/') {
            format!("{}{}", self.base_url, path)
        } else {
            format!("{}/{}", self.base_url, path)
        }
    }

    fn apply_auth(&self, builder: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        let builder = match &self.bearer {
            Some(token) => builder.bearer_auth(token),
            None => builder,
        };
        // A bad header literal is a programming bug; reqwest records it on the
        // builder and `send` reports it as `Transport`, so it stays loud
        // rather than silently dropped.
        self.extra_headers
            .iter()
            .fold(builder, |request, (name, value)| {
                request.header(name.as_str(), value.as_str())
            })
    }

    async fn round_trip(&self, builder: reqwest::RequestBuilder, timeout_secs: f64) -> HttpOutcome {
        // Per-request override (`RequestBuilder::timeout`,
        // `reqwest-0.13.4/src/async_impl/request.rs:294`). A non-positive or
        // non-finite budget cannot become a `Duration`, so it keeps the client
        // default instead of panicking inside `from_secs_f64`.
        let builder = if timeout_secs.is_finite() && timeout_secs > 0.0 {
            builder.timeout(Duration::from_secs_f64(timeout_secs))
        } else {
            builder
        };
        let response = match builder.send().await {
            Ok(response) => response,
            Err(err) => return classify(&err),
        };
        let status = response.status().as_u16();
        let text = match response.text().await {
            Ok(text) => text,
            Err(err) => return classify(&err),
        };
        if response_status_is_success(status) {
            match serde_json::from_str::<Value>(&text) {
                Ok(value) => HttpOutcome::Ok(value),
                // The server answered 2xx, so this is its answer verbatim,
                // not a transport failure; the caller reports it as a bad
                // answer (the reranker funnels it into `RerankUnavailable`).
                Err(_) => HttpOutcome::Status(status, text),
            }
        } else {
            HttpOutcome::Status(status, text)
        }
    }
}

/// One client construction for [`HttpCore`] and [`HttpAdapter`].
///
/// No retries: the Python adapters never retry (no retry policy anywhere in
/// `adapters/embedding`, `adapters/reranker`, or `adapters/llm`), and reqwest
/// only retries when `ClientBuilder::retry`
/// (`reqwest-0.13.4/src/async_impl/client.rs:1405`) is given a policy — it is
/// not, so a failure surfaces on the first attempt.
pub(crate) fn build_client(
    connect_timeout: Duration,
    request_timeout: Duration,
) -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(connect_timeout)
        .timeout(request_timeout)
        .redirect(reqwest::redirect::Policy::limited(20))
        // The default builder only fails on broken TLS/proxy setup, which is
        // a loud environment bug, not a fallback path.
        .build()
        .expect("reqwest client construction with timeouts and redirect policy")
}

fn response_status_is_success(status: u16) -> bool {
    (200..300).contains(&status)
}

/// Timeout first: a connect timeout is both a timeout and a connect error, and
/// the Python distinguishes the same way — `TimeoutException` is caught before
/// `TransportError`, which it subclasses
/// (`httpx/_exceptions.py:132` precedes `:123`). `reqwest::Error::is_timeout`
/// and `is_connect` live at `reqwest-0.13.4/src/error.rs:119` and `:153`.
fn classify(err: &reqwest::Error) -> HttpOutcome {
    if err.is_timeout() {
        HttpOutcome::Timeout(describe("Timeout", &err.to_string()))
    } else if err.is_connect() {
        HttpOutcome::Transport(describe("ConnectError", &err.to_string()))
    } else {
        HttpOutcome::Transport(describe("TransportError", &err.to_string()))
    }
}

/// Mirrors `describe_exception` (`domain/errors.py:172-184`): a description
/// that is never empty — `Type: message`, or the bare type when the message
/// is blank (which is exactly what `str(httpx.ConnectTimeout())` is).
fn describe(kind: &str, message: &str) -> String {
    let message = message.trim();
    if message.is_empty() {
        kind.to_owned()
    } else {
        format!("{kind}: {message}")
    }
}

/// Raw-bytes port of `HttpxAdapter` (`adapters/http/httpx_adapter.py`):
/// default timeout 30.0, redirects on, `get`/`post` return bytes,
/// `raise_for_status` becomes [`Error::Http`], and `close` is a noop because
/// a `reqwest::Client` holds a connection pool with nothing to release (the
/// Python `aclose` has no Rust counterpart to call).
pub struct HttpAdapter {
    client: reqwest::Client,
    default_timeout: f64,
}

impl HttpAdapter {
    pub fn new() -> Self {
        let default = Duration::from_secs_f64(HTTP_ADAPTER_DEFAULT_TIMEOUT_SECS);
        Self {
            client: build_client(default, default),
            default_timeout: HTTP_ADAPTER_DEFAULT_TIMEOUT_SECS,
        }
    }

    pub fn default_timeout_secs(&self) -> f64 {
        self.default_timeout
    }

    pub async fn get(&self, url: &str) -> Result<Vec<u8>> {
        let response = self
            .client
            .get(url)
            .timeout(Duration::from_secs_f64(self.default_timeout))
            .send()
            .await
            .map_err(|err| Error::Transport(describe("TransportError", &err.to_string())))?;
        self.response_bytes(response).await
    }

    pub async fn post(&self, url: &str, json: &Value) -> Result<Vec<u8>> {
        let response = self
            .client
            .post(url)
            .json(json)
            .timeout(Duration::from_secs_f64(self.default_timeout))
            .send()
            .await
            .map_err(|err| Error::Transport(describe("TransportError", &err.to_string())))?;
        self.response_bytes(response).await
    }

    async fn response_bytes(&self, response: reqwest::Response) -> Result<Vec<u8>> {
        let status = response.status().as_u16();
        let bytes = response
            .bytes()
            .await
            .map_err(|err| Error::Transport(describe("TransportError", &err.to_string())))?;
        if response_status_is_success(status) {
            Ok(bytes.to_vec())
        } else {
            Err(Error::Http {
                status,
                body: String::from_utf8_lossy(&bytes).into_owned(),
            })
        }
    }

    pub async fn close(&self) {}
}

impl Default for HttpAdapter {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
pub(crate) mod test_support {
    //! Loopback-only HTTP stubs for the `http`/`reranker` contract tests. No
    //! live network: everything binds `127.0.0.1` with an ephemeral port.

    use std::io::{Read, Write};
    use std::sync::Arc;

    fn reason(code: u16) -> &'static str {
        match code {
            200 => "OK",
            302 => "Found",
            400 => "Bad Request",
            404 => "Not Found",
            409 => "Conflict",
            500 => "Internal Server Error",
            _ => "Error",
        }
    }

    /// Handler shared by every stub: `Fn` boxed once so the serving body
    /// below is a single monomorphic instantiation. A generic `impl Fn`
    /// parameter would mint one `spawn_stub` copy per test closure, and
    /// `cargo llvm-cov` counts every copy's regions separately — the shared
    /// `HttpCore` doctrine above (one instantiation to pin) applies to its
    /// stubs too.
    pub(crate) type StubHandler =
        Arc<dyn Fn(String, String, String) -> (u16, Vec<(String, String)>, String) + Send + Sync>;

    /// Spawn a stub whose `handler` sees the request path, the raw head, and
    /// the body, and answers `(status, extra headers, body)`. Returns the base
    /// URL. Connections are served sequentially on a detached thread, which the
    /// OS reaps when the test binary exits.
    pub(crate) fn spawn_stub(handler: StubHandler) -> String {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind loopback stub");
        let addr = listener.local_addr().expect("stub local address");
        std::thread::spawn(move || {
            // `Incoming` errors are transient stub-harness failures (the
            // bound loopback listener outlives the test binary); skip them
            // via the iterator combinator so no defensive branch stands in
            // this file's coverage.
            for mut stream in listener.incoming().filter_map(Result::ok) {
                let mut raw = Vec::new();
                let mut byte = [0u8; 1];
                while let Ok(n) = stream.read(&mut byte) {
                    if n == 0 {
                        break;
                    }
                    raw.push(byte[0]);
                    if raw.ends_with(b"\r\n\r\n") || raw.len() > (1 << 20) {
                        break;
                    }
                }
                let head = String::from_utf8_lossy(&raw).into_owned();
                let mut lines = head.lines();
                let path = lines
                    .next()
                    .unwrap_or("")
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("/")
                    .to_owned();
                let content_length: usize = head
                    .lines()
                    .skip(1)
                    .filter_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.trim()
                            .eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().unwrap_or(0))
                    })
                    .sum();
                let mut body = vec![0u8; content_length];
                if content_length > 0 {
                    let _ = stream.read_exact(&mut body);
                }
                let (status, headers, text) =
                    handler(path, head, String::from_utf8_lossy(&body).into_owned());
                let mut response = format!(
                    "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
                    reason(status),
                    text.len()
                );
                for (name, value) in headers {
                    response.push_str(&format!("{name}: {value}\r\n"));
                }
                response.push_str("\r\n");
                response.push_str(&text);
                let _ = stream.write_all(response.as_bytes());
            }
        });
        format!("http://{addr}")
    }

    /// A port nothing listens on, for the transport-error arm. Binding then
    /// dropping a listener reserves a currently-closed loopback port.
    pub(crate) fn closed_port_base_url() -> String {
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .expect("bind probe listener")
            .local_addr()
            .expect("probe local address")
            .port();
        format!("http://127.0.0.1:{port}")
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::{closed_port_base_url, spawn_stub};
    use super::*;
    use serde_json::json;
    use std::sync::Arc;

    fn timeouts(secs: f64) -> (Duration, Duration) {
        (Duration::from_secs(5), Duration::from_secs_f64(secs))
    }

    fn core_for(base_url: &str) -> HttpCore {
        let (connect, request) = timeouts(30.0);
        HttpCore::build(base_url, None, &[], connect, request)
    }

    #[tokio::test]
    async fn http_post_and_get_return_parsed_json() {
        let base = spawn_stub(Arc::new(|path, _, _| {
            assert!(path == "/echo" || path == "/health", "unexpected {path}");
            (200, vec![], r#"{"ok":true}"#.to_owned())
        }));
        let core = core_for(&base);
        let posted = core.post_json("/echo", &json!({"q": "mishpat"}), 5.0).await;
        let fetched = core.get_json("/health", 5.0).await;
        // Debug-equality pins the value without pattern matching (a `matches!`
        // invocation leaves an untested region at the macro site).
        assert_eq!(
            format!("{posted:?}"),
            format!("{:?}", HttpOutcome::Ok(json!({"ok": true})))
        );
        assert_eq!(
            format!("{fetched:?}"),
            format!("{:?}", HttpOutcome::Ok(json!({"ok": true})))
        );
    }

    #[tokio::test]
    async fn http_status_arm_reports_code_and_body_verbatim() {
        let base = spawn_stub(Arc::new(|_, _, _| {
            (500, vec![], "boom happened".to_owned())
        }));
        let core = core_for(&base);
        let outcome = core.post_json("/echo", &json!({}), 5.0).await;
        assert_eq!(format!("{outcome:?}"), "Status(500, \"boom happened\")");
        let outcome = core.get_json("/echo", 5.0).await;
        assert_eq!(format!("{outcome:?}"), "Status(500, \"boom happened\")");
    }

    #[tokio::test]
    async fn http_redirects_are_followed() {
        let base = spawn_stub(Arc::new(|path, _, _| match path.as_str() {
            "/old" => (
                302,
                vec![("Location".to_owned(), "/new".to_owned())],
                String::new(),
            ),
            _ => (200, vec![], r#"{"done":true}"#.to_owned()),
        }));
        let core = core_for(&base);
        let outcome = core.post_json("/old", &json!({}), 5.0).await;
        assert_eq!(
            format!("{outcome:?}"),
            format!("{:?}", HttpOutcome::Ok(json!({"done": true})))
        );
    }

    #[tokio::test]
    async fn http_redirect_loop_surfaces_as_transport_not_timeout() {
        let base = spawn_stub(Arc::new(|_, _, _| {
            (
                302,
                vec![("Location".to_owned(), "/loop".to_owned())],
                String::new(),
            )
        }));
        let core = core_for(&base);
        // The exhausted redirect policy is neither a timeout nor a connect
        // failure, pinning the fallthrough arm of `classify`.
        assert!(format!("{:?}", core.get_json("/loop", 5.0).await).starts_with("Transport("));
    }

    #[tokio::test]
    async fn http_bare_paths_join_against_the_base() {
        let base = spawn_stub(Arc::new(|path, _, _| {
            assert_eq!(path, "/joined");
            (200, vec![], r#"{"ok":true}"#.to_owned())
        }));
        let core = core_for(&base);
        assert!(format!("{:?}", core.get_json("joined", 5.0).await).starts_with("Ok("));
    }

    #[tokio::test]
    async fn http_slow_server_hits_the_timeout_arm() {
        let base = spawn_stub(Arc::new(|_, _, _| {
            std::thread::sleep(Duration::from_millis(1500));
            (200, vec![], r#"{"ok":true}"#.to_owned())
        }));
        let core = core_for(&base);
        assert!(format!("{:?}", core.get_json("/slow", 0.15).await).starts_with("Timeout("));
    }

    #[tokio::test]
    async fn http_unreachable_host_hits_the_transport_arm() {
        let core = core_for(&closed_port_base_url());
        let outcome = core.post_json("/rerank", &json!({}), 2.0).await;
        assert!(format!("{outcome:?}").contains("ConnectError: "));
    }

    #[tokio::test]
    async fn http_non_positive_timeout_keeps_the_client_default() {
        let base = spawn_stub(Arc::new(|_, _, _| {
            (200, vec![], r#"{"ok":true}"#.to_owned())
        }));
        let core = core_for(&base);
        assert!(format!("{:?}", core.get_json("/x", 0.0).await).starts_with("Ok("));
        assert!(format!("{:?}", core.get_json("/x", f64::NAN).await).starts_with("Ok("));
    }

    #[test]
    fn http_default_matches_new() {
        assert_eq!(
            HttpAdapter::default().default_timeout_secs(),
            HTTP_ADAPTER_DEFAULT_TIMEOUT_SECS
        );
    }

    #[test]
    fn http_build_trims_the_base_and_describe_never_empties() {
        let (connect, request) = timeouts(30.0);
        let core = HttpCore::build("http://host:9///", None, &[], connect, request);
        assert_eq!(core.base_url, "http://host:9");
        assert_eq!(describe("ConnectError", ""), "ConnectError");
        assert_eq!(describe("ConnectError", "  "), "ConnectError");
        assert_eq!(describe("X", "m"), "X: m");
        assert_eq!(describe("X", "  m  "), "X: m");
    }

    #[tokio::test]
    async fn http_bearer_and_extra_headers_reach_the_server() {
        let base = spawn_stub(Arc::new(|_, head, _| {
            let head = head.to_ascii_lowercase();
            // `reqwest` renders `Bearer <token>` exactly like the Python
            // `{"Authorization": f"Bearer {api_key}"}` default headers (names
            // compare case-insensitively, so the stub lowercases the head).
            if head.contains("authorization: bearer secret\r\n") && head.contains("x-test: 1\r\n") {
                (200, vec![], r#"{"ok":true}"#.to_owned())
            } else {
                (400, vec![], "missing auth".to_owned())
            }
        }));
        let (connect, request) = timeouts(30.0);
        let core = HttpCore::build(&base, Some("secret"), &[("x-test", "1")], connect, request);
        assert!(format!("{:?}", core.post_json("/echo", &json!({}), 5.0).await).starts_with("Ok("));
        // Without the headers the same stub answers 400, firing its else arm.
        let (connect, request) = timeouts(30.0);
        let bare = HttpCore::build(&base, None, &[], connect, request);
        assert_eq!(
            format!("{:?}", bare.post_json("/echo", &json!({}), 5.0).await),
            "Status(400, \"missing auth\")"
        );
    }

    #[tokio::test]
    async fn http_adapter_contract_get_post_status_close() {
        use std::sync::{Arc, Mutex};

        assert_eq!(HTTP_ADAPTER_DEFAULT_TIMEOUT_SECS, 30.0);
        let seen = Arc::new(Mutex::new(String::new()));
        let probe = Arc::clone(&seen);
        let base = spawn_stub(Arc::new(move |path, _, body| {
            if path == "/missing" {
                return (404, vec![], "nope".to_owned());
            }
            *probe.lock().expect("probe lock") = body;
            (200, vec![], r#"{"seen":true}"#.to_owned())
        }));
        let adapter = HttpAdapter::new();
        assert_eq!(adapter.default_timeout_secs(), 30.0);

        let bytes = adapter
            .get(&format!("{base}/items"))
            .await
            .expect("stub get");
        assert_eq!(bytes, br#"{"seen":true}"#.as_slice());
        adapter
            .post(&format!("{base}/items"), &json!({"hello": "world"}))
            .await
            .expect("stub post");
        // `reqwest::json` serializes without spaces, so the compact form
        // is the contract (mirroring `json.dumps` separators on the Python side).
        assert!(
            seen.lock()
                .expect("probe lock")
                .contains(r#""hello":"world""#),
            "posted body never arrived"
        );

        let err = adapter
            .get(&format!("{base}/missing"))
            .await
            .expect_err("404 must fail");
        assert_eq!(format!("{err:?}"), "Http { status: 404, body: \"nope\" }");
        let err = HttpAdapter::new()
            .get(&format!("{}/gone", closed_port_base_url()))
            .await
            .expect_err("closed port must fail");
        assert!(format!("{err:?}").starts_with("Transport("));
        // POST send has its own transport arm (`post`'s `map_err`): a closed
        // port fires it the same way, mirroring the Python `post` raising
        // `TransportError` like `get` does.
        let err = HttpAdapter::new()
            .post(&format!("{}/gone", closed_port_base_url()), &json!({}))
            .await
            .expect_err("closed-port post must fail");
        assert!(format!("{err:?}").starts_with("Transport("));
        // `close` is a no-op over a pooled client; repeated calls are safe.
        adapter.close().await;
        adapter.close().await;
        HttpAdapter::default().close().await;
    }

    #[tokio::test]
    async fn http_success_with_non_json_body_reports_status_verbatim() {
        // The server answered 2xx, so its answer is verbatim — not a
        // transport failure. The caller reports it as a bad answer (the
        // reranker funnels it into `RerankUnavailable`). Mirrors a Python
        // `resp.json()` `JSONDecodeError` on a 200.
        let base = spawn_stub(Arc::new(|_, _, _| (200, vec![], "not json".to_owned())));
        let core = core_for(&base);
        assert_eq!(
            format!("{:?}", core.get_json("/ok", 5.0).await),
            "Status(200, \"not json\")"
        );
    }

    #[tokio::test]
    async fn http_core_truncated_body_is_transport() {
        use std::io::Write;

        // Promise 100 bytes, deliver 10, then close: `response.text()` can
        // never complete, firing the `Err(err) => classify` arm in
        // `round_trip` (the adapter-level truncation test below covers the
        // parallel arm in `response_bytes`).
        let listener =
            std::net::TcpListener::bind("127.0.0.1:0").expect("bind core truncation stub");
        let addr = listener.local_addr().expect("core truncation stub address");
        std::thread::spawn(move || {
            for mut stream in listener.incoming().filter_map(Result::ok) {
                let mut discard = [0u8; 4096];
                let _ = std::io::Read::read(&mut stream, &mut discard);
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n0123456789",
                );
            }
        });
        let core = core_for(&format!("http://{addr}"));
        assert!(format!("{:?}", core.get_json("/short", 5.0).await).starts_with("Transport("));
    }

    #[tokio::test]
    async fn http_reason_phrases_cover_error_statuses() {
        // Pins the stub's reason table: 400 and an unknown code (the `_`
        // fallthrough). Other codes ride the redirect/status/contract tests.
        for (status, body) in [(400u16, "bad"), (418u16, "teapot")] {
            let base = spawn_stub(Arc::new(move |_, _, _| (status, vec![], body.to_owned())));
            let core = core_for(&base);
            let outcome = core.get_json("/x", 5.0).await;
            assert_eq!(
                format!("{outcome:?}"),
                format!("Status({status}, {body:?})")
            );
        }
    }

    #[tokio::test]
    async fn http_client_disconnect_breaks_header_read() {
        // A client that connects and closes without sending forces the
        // stub's header `n == 0` break; the stub must keep serving afterwards.
        let base = spawn_stub(Arc::new(|_, _, _| {
            (200, vec![], r#"{"ok":true}"#.to_owned())
        }));
        let addr = base.trim_start_matches("http://").to_owned();
        let socket = std::net::TcpStream::connect(&addr).expect("connect stub");
        drop(socket);
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        // A head that never terminates forces the 1 MiB overflow guard to
        // break the header loop (mirroring a client that streams garbage):
        // the stub answers from the partial head and keeps serving.
        let mut big = std::net::TcpStream::connect(&addr).expect("connect stub");
        {
            use std::io::Write;
            big.write_all(b"GET /x HTTP/1.1\r\nhost: x\r\nx-pad: ")
                .expect("write big head");
            big.write_all(&vec![b'x'; (1 << 20) + 16])
                .expect("write big pad");
        }
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
        let core = core_for(&base);
        assert!(format!("{:?}", core.get_json("/x", 5.0).await).starts_with("Ok("));
    }

    #[tokio::test]
    async fn http_post_bare_path_joins_against_the_base() {
        // `post_json` joins bare paths exactly like `get_json` (shared
        // `url`), mirroring clients that post absolute paths like `/rerank`.
        let base = spawn_stub(Arc::new(|path, _, _| {
            assert_eq!(path, "/joined");
            (200, vec![], r#"{"ok":true}"#.to_owned())
        }));
        let core = core_for(&base);
        assert!(
            format!("{:?}", core.post_json("joined", &json!({}), 5.0).await).starts_with("Ok(")
        );
    }

    #[tokio::test]
    async fn http_negative_and_infinite_timeouts_keep_the_client_default() {
        // A non-positive or non-finite budget cannot become a `Duration`, so
        // it keeps the client default instead of panicking inside
        // `from_secs_f64` — mirroring the reranker's `timeout_secs.max(0.0)`
        // guard on construction.
        let base = spawn_stub(Arc::new(|_, _, _| {
            (200, vec![], r#"{"ok":true}"#.to_owned())
        }));
        let core = core_for(&base);
        assert!(format!("{:?}", core.get_json("/x", -1.0).await).starts_with("Ok("));
        assert!(format!("{:?}", core.get_json("/x", f64::INFINITY).await).starts_with("Ok("));
        assert!(format!("{:?}", core.get_json("/x", f64::NEG_INFINITY).await).starts_with("Ok("));
    }

    #[tokio::test]
    async fn http_adapter_truncated_body_is_transport() {
        use std::io::Write;

        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind truncation stub");
        let addr = listener.local_addr().expect("truncation stub address");
        std::thread::spawn(move || {
            // `Incoming` errors are transient stub-harness failures (the
            // bound loopback listener outlives the test binary); skip them
            // via the iterator combinator so no defensive branch stands in
            // this file's coverage.
            for mut stream in listener.incoming().filter_map(Result::ok) {
                let mut discard = [0u8; 4096];
                let _ = std::io::Read::read(&mut stream, &mut discard);
                // Promise 100 bytes, deliver 10, then close: the body can
                // never complete, so `bytes()` must fail.
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n0123456789",
                );
            }
        });
        let err = HttpAdapter::new()
            .get(&format!("http://{addr}/short"))
            .await
            .expect_err("truncated body must fail");
        assert!(format!("{err:?}").starts_with("Transport("));
    }
}
