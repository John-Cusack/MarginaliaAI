//! Remote embedding client: port of `adapters/embedding/remote_api.py`.
//!
//! An embedding-port backend that offloads inference to a GPU host running
//! `research-engine embed-server` over HTTP. Model identity is verified once,
//! on the first call, against the server's `/health`; until that handshake
//! succeeds no vector is stored.
//!
//! **A backend failure here means "fail", not "embed locally".** A vector is
//! only comparable to vectors from the same model, so silently switching
//! models mid-run would write points that no index can relate to each other.
//! When the server is unreachable this raises, the batch fails, and
//! `research-engine embeddings backfill` picks it up later against a model
//! that is known to match.

use std::time::Duration;

use reqwest::{Client, Method};

use crate::errors::{Error, Result};
use crate::wire::{EmbedRequest, EmbedResponse, HealthResponse};

/// Consecutive failures before the circuit opens and calls fail fast instead
/// of each waiting out the timeout. A corpus-wide run issues thousands of
/// calls; without this, a server that died mid-run costs `timeout x batches`
/// before anyone notices.
pub const FAILURE_THRESHOLD: u32 = 3;

/// `GET /health` deadline. A client that cannot get a timely `/health` cannot
/// distinguish a busy server from a dead one.
pub const HEALTH_TIMEOUT: Duration = Duration::from_secs(10);

/// Detail carried by the dimension-mismatch refusal. A dimension disagreement
/// would trip the `vector(N)` column; a same-dimension model swap would slip
/// past every constraint, so the message says exactly that.
///
/// [`Error::ModelMismatch`] carries identity only, so this is rendered at the
/// raise site through [`crate::errors::model_mismatch_message`]; the refusal
/// test pins the full rendering.
pub const DIM_MISMATCH_DETAIL: &str = "Dimension disagreement would be caught by the vector(N) column; a same-dimension model mismatch would not.";

/// An embedding-port backend served by a remote `research-engine embed-server`.
pub struct RemoteEmbeddingClient {
    base_url: String,
    model_name: String,
    model_version: String,
    dim: i64,
    timeout: Duration,
    api_key: Option<String>,
    client: Client,
    verified: bool,
    consecutive_failures: u32,
    failure_threshold: u32,
    last_version_drift: bool,
}

/// Backwards-compatible alias. The old name described a stub that spoke a
/// generic OpenAI-shaped API and verified nothing.
pub type RemoteAPIEmbedding = RemoteEmbeddingClient;

fn build_client(timeout: Duration, api_key: Option<&str>) -> Result<Client> {
    let mut headers = reqwest::header::HeaderMap::new();
    if let Some(key) = api_key {
        let value = reqwest::header::HeaderValue::from_str(&format!("Bearer {key}"))
            .map_err(|err| Error::Validation(format!("invalid API key: {err}")))?;
        headers.insert(reqwest::header::AUTHORIZATION, value);
    }
    Ok(Client::builder()
        // httpx `timeout=X` sets its connect, read, write, and pool timeouts
        // all to X; reqwest splits them — `.timeout()` is the whole-request
        // deadline while the connect phase needs `.connect_timeout()`
        // separately — so pin both to the same value.
        .connect_timeout(timeout)
        .timeout(timeout)
        // Python never retries: a transport error is fatal on the first call
        // (the handshake) or counted once toward the breaker (a batch), so no
        // retry middleware belongs here either.
        .default_headers(headers)
        // The builder only fails on broken TLS/proxy setup, which is a loud
        // environment bug, not a fallback path (same rationale as
        // `http::build_client`'s `expect`): with valid timeouts and headers
        // construction cannot fail, so the `Err` arm of `build` is dead here.
        // It stays loud via `expect` rather than becoming an untested `?` arm.
        .build()
        .expect("reqwest client construction with timeouts and bearer headers"))
}

/// Mirror of `describe_exception` (`{Type}: {msg}`, bare `Type` when the
/// message is empty): several transport errors carry no message, and an empty
/// cause once made embedding runs log `error=` for hours.
fn describe_transport(err: &reqwest::Error) -> String {
    let kind = if err.is_timeout() {
        "Timeout"
    } else if err.is_connect() {
        "ConnectError"
    } else {
        "TransportError"
    };
    join_kind_message(kind, err.to_string().trim())
}

fn join_kind_message(kind: &str, message: &str) -> String {
    if message.is_empty() {
        kind.to_owned()
    } else {
        format!("{kind}: {message}")
    }
}

impl RemoteEmbeddingClient {
    /// Point at `base_url` with the Python defaults: version `1.0`, dim 1024,
    /// 120 s timeout, no API key.
    pub fn new(base_url: impl Into<String>, model: impl Into<String>) -> Result<Self> {
        let timeout = Duration::from_secs_f64(120.0);
        Ok(Self {
            base_url: base_url.into().trim_end_matches('/').to_owned(),
            model_name: model.into(),
            model_version: "1.0".to_owned(),
            dim: 1024,
            timeout,
            api_key: None,
            // Infallible here: `api_key` is `None`, so no header is parsed,
            // and client construction itself is `expect`ed inside
            // `build_client`. The `Err` arm is dead on this path.
            client: build_client(timeout, None).expect("keyless client builds"),
            verified: false,
            consecutive_failures: 0,
            failure_threshold: FAILURE_THRESHOLD,
            last_version_drift: false,
        })
    }

    pub fn with_dim(mut self, dim: i64) -> Self {
        self.dim = dim;
        self
    }

    pub fn with_model_version(mut self, version: impl Into<String>) -> Self {
        self.model_version = version.into();
        self
    }

    pub fn with_timeout_secs(mut self, secs: f64) -> Result<Self> {
        let timeout = Duration::from_secs_f64(secs);
        // Infallible here: the stored key (if any) passed
        // `HeaderValue::from_str` validation when `with_api_key` stored it,
        // and validation is deterministic — a key that parsed once parses
        // again. The `Err` arm is dead on this path.
        self.client =
            build_client(timeout, self.api_key.as_deref()).expect("stored key revalidates");
        self.timeout = timeout;
        Ok(self)
    }

    pub fn with_api_key(mut self, api_key: impl Into<String>) -> Result<Self> {
        let api_key = api_key.into();
        self.client = build_client(self.timeout, Some(&api_key))?;
        self.api_key = Some(api_key);
        Ok(self)
    }

    pub fn with_failure_threshold(mut self, threshold: u32) -> Self {
        self.failure_threshold = threshold;
        self
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn model_name(&self) -> &str {
        &self.model_name
    }

    pub fn model_version(&self) -> &str {
        &self.model_version
    }

    pub fn dim(&self) -> i64 {
        self.dim
    }

    pub async fn embed(&mut self, text: &str) -> Result<Vec<f64>> {
        Ok(self.embed_batch(&[text.to_owned()]).await?.remove(0))
    }

    pub async fn embed_batch(&mut self, texts: &[String]) -> Result<Vec<Vec<f64>>> {
        if texts.is_empty() {
            return Ok(Vec::new());
        }
        // Before the handshake, not after. `ensure_verified` dials /health, so
        // checking the circuit second meant a corpus run against a powered-off
        // host paid a connect timeout per batch — the exact cost the breaker
        // exists to avoid.
        self.check_circuit()?;
        self.ensure_verified().await?;
        // No second circuit check: every `ensure_verified` failure path that
        // increments `consecutive_failures` returns `Err` (the `?` above
        // propagates it before a second check could run), and success paths
        // never increment — so a second check observes the same counter the
        // first check passed. (`&mut self` proves no concurrent task can
        // increment between the two, unlike the Python `asyncio` client where
        // aliases share one counter behind a lock.) The check is dead here.

        // `EmbedRequest` carries only strings, ints, and `None`s, so
        // serialization cannot fail (mirroring the reranker's
        // `expect("strings serialize")`); the `Err` arm of `to_value` is dead.
        let body = serde_json::to_value(EmbedRequest {
            texts: texts.to_vec(),
            expect_model: Some(self.model_name.clone()),
            expect_dim: Some(self.dim),
        })
        .expect("strings serialize");
        let value = match self
            .request_json(Method::POST, "/embeddings", Some(body), self.timeout)
            .await
        {
            Ok(value) => {
                self.consecutive_failures = 0;
                value
            }
            // Never reached the server, so batch size had nothing to do with
            // it. Translated here, at the layer that knows about transports,
            // so the caller can tell "the box is off" from "that batch was
            // too big".
            Err(Error::Transport(detail)) => {
                self.consecutive_failures += 1;
                return Err(Error::EmbeddingUnavailable(format!(
                    "Cannot reach the embedding server at {}: {detail}",
                    self.base_url
                )));
            }
            Err(err @ Error::Http { .. }) => {
                self.consecutive_failures += 1;
                return Err(err);
            }
            Err(err) => return Err(err),
        };

        let response: EmbedResponse = serde_json::from_value(value)
            .map_err(|err| Error::Json(format!("invalid /embeddings response: {err}")))?;
        self.assert_matches(&response.model_name, &response.model_version, response.dim)?;
        if response.embeddings.len() != texts.len() {
            return Err(Error::Validation(format!(
                "Remote server returned {} embeddings for {} texts.",
                response.embeddings.len(),
                texts.len()
            )));
        }
        Ok(response.embeddings)
    }

    fn check_circuit(&self) -> Result<()> {
        if self.consecutive_failures >= self.failure_threshold {
            return Err(Error::EmbeddingUnavailable(format!(
                "Remote embedding server {} failed {} times consecutively; refusing \
                 further calls. Fix the server, or set RE_EMBEDDING_PROVIDER=local_bge \
                 to embed on this machine, then run `research-engine embeddings backfill`.",
                self.base_url, self.consecutive_failures
            )));
        }
        Ok(())
    }

    /// No-op: `reqwest::Client` owns its connection pool and is cheap to
    /// clone, so there is no per-client socket to drain the way
    /// `httpx.AsyncClient.aclose()` drains one.
    pub async fn close(&self) {}

    /// `GET /health` — identity and readiness of the served models.
    pub async fn health(&self) -> Result<HealthResponse> {
        let value = self
            .request_json(Method::GET, "/health", None, HEALTH_TIMEOUT)
            .await?;
        serde_json::from_value(value)
            .map_err(|err| Error::Json(format!("invalid /health response: {err}")))
    }

    /// Confirm once that the server serves the model this corpus uses.
    ///
    /// Single-owner `&mut self`, so no lock: the Python side needed an
    /// `asyncio.Lock` because aliases of one client could race the handshake;
    /// here the borrow checker proves sole access.
    async fn ensure_verified(&mut self) -> Result<()> {
        if self.verified {
            return Ok(());
        }
        let health = match self.health().await {
            Ok(health) => health,
            Err(Error::Transport(detail)) => {
                // The handshake is the *first* call a run makes, so an
                // unreachable host fails here rather than in `embed_batch`.
                // Untranslated it surfaced as a bare ConnectTimeout and was
                // mistaken for a batch that needed halving.
                self.consecutive_failures += 1;
                return Err(Error::EmbeddingUnavailable(format!(
                    "Cannot reach the embedding server at {}: {detail}. Check the host \
                     is powered on and `research-engine embed-server` is running.",
                    self.base_url
                )));
            }
            Err(err) => return Err(err),
        };
        self.assert_matches(&health.model_name, &health.model_version, health.dim)?;
        self.verified = true;
        Ok(())
    }

    fn assert_matches(&mut self, name: &str, version: &str, dim: i64) -> Result<()> {
        if name != self.model_name {
            return Err(Error::ModelMismatch {
                expected: self.model_name.clone(),
                actual: name.to_owned(),
                kind: "embedding".to_owned(),
            });
        }
        if dim != self.dim {
            return Err(Error::ModelMismatch {
                expected: format!("{} (dim {})", self.model_name, self.dim),
                actual: format!("{name} (dim {dim})"),
                kind: "embedding".to_owned(),
            });
        }
        // Not fatal on its own — a version bump may be a packaging change —
        // but it is exactly the kind of drift that explains a later recall
        // regression, so it must not pass silently.
        self.last_version_drift = version != self.model_version;
        Ok(())
    }

    /// The single HTTP boundary in this module. It stays monomorphic (concrete
    /// `serde_json::Value` in and out) on purpose: a generic helper would need
    /// its error arms fired in every test binary that instantiates it, while
    /// callers deserializing concrete wire types add no such burden.
    async fn request_json(
        &self,
        method: Method,
        path: &str,
        body: Option<serde_json::Value>,
        timeout: Duration,
    ) -> Result<serde_json::Value> {
        let url = format!("{}{}", self.base_url, path);
        let mut request = match method {
            Method::POST => self.client.post(&url),
            _ => self.client.get(&url),
        }
        .timeout(timeout);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request
            .send()
            .await
            .map_err(|err| Error::Transport(describe_transport(&err)))?;
        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(Error::Http {
                status: status.as_u16(),
                body,
            });
        }
        response
            .json::<serde_json::Value>()
            .await
            .map_err(|err| Error::Json(format!("invalid JSON from {url}: {err}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::errors::model_mismatch_message;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    };
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };

    const MODEL: &str = "BAAI/bge-m3";
    const DIM: i64 = 8;

    fn health_json(model: &str, dim: i64, version: &str) -> serde_json::Value {
        serde_json::json!({
            "status": "ok",
            "model_name": model,
            "model_version": version,
            "dim": dim,
            "device": "cpu",
            "warm": true,
            "concurrency": 1,
        })
    }

    enum EmbedBehavior {
        /// Answer with per-text lengths, like the Python stub backend.
        Echo,
        /// Answer every POST with a fixed status and body.
        Status(u16, serde_json::Value),
        /// Answer every POST with verbatim bytes (for non-JSON 2xx arms).
        Raw(u16, Vec<u8>),
        /// Accept the POST and never answer within the test's patience.
        Hang,
        /// Close the connection without answering.
        Drop,
    }

    /// Canned HTTP/1.1 stub: no framework, loopback only.
    struct Canned {
        base_url: String,
        health_hits: Arc<AtomicUsize>,
        embed_hits: Arc<AtomicUsize>,
        auth_headers: Arc<Mutex<Vec<Option<String>>>>,
        embed_bodies: Arc<Mutex<Vec<serde_json::Value>>>,
        task: tokio::task::JoinHandle<()>,
    }

    impl Drop for Canned {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    async fn spawn_canned(health: serde_json::Value, behavior: EmbedBehavior) -> Canned {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let health = Arc::new(health);
        let behavior = Arc::new(behavior);
        let health_hits = Arc::new(AtomicUsize::new(0));
        let embed_hits = Arc::new(AtomicUsize::new(0));
        let auth_headers = Arc::new(Mutex::new(Vec::new()));
        let embed_bodies = Arc::new(Mutex::new(Vec::new()));
        let task = {
            let health = Arc::clone(&health);
            let behavior = Arc::clone(&behavior);
            let health_hits = Arc::clone(&health_hits);
            let embed_hits = Arc::clone(&embed_hits);
            let auth_headers = Arc::clone(&auth_headers);
            let embed_bodies = Arc::clone(&embed_bodies);
            tokio::task::spawn(async move {
                loop {
                    // `accept` on a bound loopback listener is infallible in
                    // these tests: the listener outlives the stub task (it is
                    // aborted via `Canned::drop`), so an error here would mean
                    // the harness itself broke. Panic loudly rather than
                    // silently ending the stub, which would hang the test.
                    let (socket, _) = listener.accept().await.expect("loopback stub accept");
                    let health = Arc::clone(&health);
                    let behavior = Arc::clone(&behavior);
                    let health_hits = Arc::clone(&health_hits);
                    let embed_hits = Arc::clone(&embed_hits);
                    let auth_headers = Arc::clone(&auth_headers);
                    let embed_bodies = Arc::clone(&embed_bodies);
                    tokio::task::spawn(async move {
                        serve_one(
                            socket,
                            &health,
                            &behavior,
                            &health_hits,
                            &embed_hits,
                            &auth_headers,
                            &embed_bodies,
                        )
                        .await;
                    });
                }
            })
        };
        Canned {
            base_url,
            health_hits,
            embed_hits,
            auth_headers,
            embed_bodies,
            task,
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn serve_one(
        mut socket: tokio::net::TcpStream,
        health: &serde_json::Value,
        behavior: &EmbedBehavior,
        health_hits: &AtomicUsize,
        embed_hits: &AtomicUsize,
        auth_headers: &Mutex<Vec<Option<String>>>,
        embed_bodies: &Mutex<Vec<serde_json::Value>>,
    ) {
        let mut buf = Vec::new();
        let mut tmp = [0u8; 4096];
        loop {
            let n = socket.read(&mut tmp).await.unwrap_or(0);
            if n == 0 {
                return;
            }
            buf.extend_from_slice(&tmp[..n]);
            if buf.windows(4).any(|w| w == b"\r\n\r\n") {
                break;
            }
        }
        let head_end = buf
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .map(|pos| pos + 4)
            .unwrap_or(buf.len());
        let head = String::from_utf8_lossy(&buf[..head_end]).into_owned();
        let mut lines = head.lines();
        let request_line = lines.next().unwrap_or("").to_owned();
        let mut content_length = 0usize;
        let mut auth: Option<String> = None;
        for line in lines {
            if let Some((name, value)) = line.split_once(':') {
                if name.trim().eq_ignore_ascii_case("content-length") {
                    content_length = value.trim().parse().unwrap_or(0);
                } else if name.trim().eq_ignore_ascii_case("authorization") {
                    auth = Some(value.trim().to_owned());
                }
            }
        }
        let mut body = buf[head_end..].to_vec();
        while body.len() < content_length {
            let n = socket.read(&mut tmp).await.unwrap_or(0);
            if n == 0 {
                break;
            }
            body.extend_from_slice(&tmp[..n]);
        }

        if request_line.starts_with("GET /health") {
            health_hits.fetch_add(1, Ordering::SeqCst);
            write_json(&mut socket, 200, &serde_json::to_vec(health).unwrap()).await;
        } else if request_line.starts_with("POST /embeddings") {
            embed_hits.fetch_add(1, Ordering::SeqCst);
            auth_headers.lock().unwrap().push(auth);
            let seen: serde_json::Value =
                serde_json::from_slice(&body).unwrap_or(serde_json::Value::Null);
            embed_bodies.lock().unwrap().push(seen.clone());
            match behavior {
                EmbedBehavior::Echo => {
                    let dim = health
                        .get("dim")
                        .and_then(serde_json::Value::as_i64)
                        .unwrap_or(DIM);
                    let model = health
                        .get("model_name")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or(MODEL);
                    let version = health
                        .get("model_version")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("1.0");
                    let texts = seen
                        .get("texts")
                        .and_then(serde_json::Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    let embeddings: Vec<Vec<f64>> = texts
                        .iter()
                        .map(|text| {
                            let n = text.as_str().map(|text| text.len() as f64).unwrap_or(0.0);
                            vec![n; dim as usize]
                        })
                        .collect();
                    write_json(
                        &mut socket,
                        200,
                        &serde_json::to_vec(&serde_json::json!({
                            "embeddings": embeddings,
                            "model_name": model,
                            "model_version": version,
                            "dim": dim,
                        }))
                        .unwrap(),
                    )
                    .await;
                }
                EmbedBehavior::Status(status, value) => {
                    write_json(&mut socket, *status, &serde_json::to_vec(value).unwrap()).await;
                }
                EmbedBehavior::Raw(status, raw) => {
                    write_raw(&mut socket, *status, raw).await;
                }
                EmbedBehavior::Hang => {
                    // Never answer: the client times out and drops its end,
                    // so any write here would be unobservable (and would race
                    // the stub's abort on `Canned::drop`). Sleep past every
                    // test's patience instead of writing.
                    tokio::time::sleep(Duration::from_secs(30)).await;
                }
                EmbedBehavior::Drop => {}
            }
        } else {
            write_json(&mut socket, 404, br#"{"detail":"not found"}"#).await;
        }
    }

    async fn write_json(socket: &mut tokio::net::TcpStream, status: u16, body: &[u8]) {
        let reason = match status {
            200 => "OK",
            409 => "Conflict",
            500 => "Internal Server Error",
            503 => "Service Unavailable",
            _ => "Error",
        };
        let head = format!(
            "HTTP/1.1 {status} {reason}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
            body.len()
        );
        // Best-effort: disconnect probes close before the answer arrives, and
        // a broken pipe there must not panic the stub task.
        let _ = socket.write_all(head.as_bytes()).await;
        let _ = socket.write_all(body).await;
    }

    async fn write_raw(socket: &mut tokio::net::TcpStream, status: u16, body: &[u8]) {
        let head = format!(
            "HTTP/1.1 {status} Error\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
            body.len()
        );
        let _ = socket.write_all(head.as_bytes()).await;
        let _ = socket.write_all(body).await;
    }

    /// A loopback URL whose port is closed: bind, read the port, drop.
    async fn closed_base_url() -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        format!("http://127.0.0.1:{port}")
    }

    fn client_for(base: &str) -> RemoteEmbeddingClient {
        RemoteEmbeddingClient::new(base, MODEL)
            .unwrap()
            .with_dim(DIM)
    }

    fn texts(words: &[&str]) -> Vec<String> {
        words.iter().map(ToString::to_string).collect()
    }

    #[tokio::test]
    async fn round_trip() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let mut client = client_for(&server.base_url);
        let vectors = client
            .embed_batch(&texts(&["alpha", "beta"]))
            .await
            .unwrap();
        assert_eq!(
            vectors,
            vec![vec![5.0; DIM as usize], vec![4.0; DIM as usize]]
        );
        let single = client.embed("alpha").await.unwrap();
        assert_eq!(single, vec![5.0; DIM as usize]);
        // Handshake-once caching: the second call sends no GET. Until the
        // handshake succeeds no vector is stored, and after it succeeds no
        // further `/health` is dialled.
        assert_eq!(server.health_hits.load(Ordering::SeqCst), 1);
        assert_eq!(server.embed_hits.load(Ordering::SeqCst), 2);
        assert_eq!(client.consecutive_failures, 0);
        // `close` is a no-op over a pooled client; it must be idempotent.
        client.close().await;
        client.close().await;
        let bodies = server.embed_bodies.lock().unwrap();
        assert_eq!(bodies.len(), 2);
        assert_eq!(bodies[0]["texts"], serde_json::json!(["alpha", "beta"]));
        assert_eq!(bodies[0]["expect_model"], serde_json::json!(MODEL));
        assert_eq!(bodies[0]["expect_dim"], serde_json::json!(DIM));
    }

    #[tokio::test]
    async fn health_reports_model_identity() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let client = client_for(&server.base_url);
        let health = client.health().await.unwrap();
        assert_eq!(health.model_name, MODEL);
        assert_eq!(health.dim, DIM);
        assert_eq!(health.status, "ok");
    }

    #[tokio::test]
    async fn client_refuses_a_different_model() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let mut client = RemoteEmbeddingClient::new(&server.base_url, "some-other-model")
            .unwrap()
            .with_dim(DIM);
        let err = client.embed_batch(&texts(&["alpha"])).await.unwrap_err();
        assert_eq!(
            err,
            Error::ModelMismatch {
                expected: "some-other-model".to_owned(),
                actual: MODEL.to_owned(),
                kind: "embedding".to_owned(),
            }
        );
        assert_eq!(
            model_mismatch_message("some-other-model", MODEL, "", "embedding"),
            "Remote embedding server serves 'BAAI/bge-m3', but this corpus expects \
             'some-other-model'."
        );
    }

    #[tokio::test]
    async fn server_refusal_reaches_the_caller_as_409() {
        let detail = "This server serves 'BAAI/bge-m3'; the client expects 'wrong-model'. \
                      Vectors from different models are not comparable — refusing rather \
                      than serving them.";
        let server = spawn_canned(
            health_json(MODEL, DIM, "1.0"),
            EmbedBehavior::Status(409, serde_json::json!({ "detail": detail })),
        )
        .await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        assert!(err.to_string().starts_with("HTTP error 409: "));
        assert!(err.to_string().contains("not comparable"));
        assert_eq!(client.consecutive_failures, 1);
    }

    #[tokio::test]
    async fn dimension_mismatch_is_refused() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let mut client = RemoteEmbeddingClient::new(&server.base_url, MODEL)
            .unwrap()
            .with_dim(1024);
        let err = client.embed_batch(&texts(&["alpha"])).await.unwrap_err();
        // Exact Debug pins expected/actual/kind without pattern matching (a
        // `match` extraction would leave an untested arm standing).
        assert_eq!(
            format!("{err:?}"),
            "ModelMismatch { expected: \"BAAI/bge-m3 (dim 1024)\", actual: \"BAAI/bge-m3 (dim 8)\", kind: \"embedding\" }"
        );
        assert_eq!(
            model_mismatch_message(
                "BAAI/bge-m3 (dim 1024)",
                &format!("{MODEL} (dim {DIM})"),
                DIM_MISMATCH_DETAIL,
                "embedding"
            ),
            "Remote embedding server serves 'BAAI/bge-m3 (dim 8)', but this corpus expects \
             'BAAI/bge-m3 (dim 1024)'. Dimension disagreement would be caught by the \
             vector(N) column; a same-dimension model mismatch would not."
        );
    }

    #[tokio::test]
    async fn nothing_is_returned_before_the_handshake_succeeds() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let mut client = RemoteEmbeddingClient::new(&server.base_url, "mismatched")
            .unwrap()
            .with_dim(DIM);
        let err = client.embed_batch(&texts(&["alpha"])).await.unwrap_err();
        assert!(format!("{err:?}").starts_with("ModelMismatch { "));
        assert_eq!(server.embed_hits.load(Ordering::SeqCst), 0);
        assert_eq!(server.health_hits.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn transport_failure_is_fatal_on_the_first_call() {
        let mut client = client_for(&closed_base_url().await);
        client.verified = true; // skip the handshake for this test
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        let message = err.to_string();
        assert!(format!("{err:?}").starts_with("EmbeddingUnavailable("));
        assert!(message.contains("Cannot reach"), "unexpected: {message}");
        assert!(message.contains(&client.base_url), "must name the server");
        assert_eq!(client.consecutive_failures, 1);
    }

    #[tokio::test]
    async fn hang_reports_a_timeout_kind() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Hang).await;
        let mut client = client_for(&server.base_url)
            .with_timeout_secs(0.05)
            .unwrap();
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        let message = err.to_string();
        assert!(format!("{err:?}").starts_with("EmbeddingUnavailable("));
        assert!(message.contains("Timeout"), "unexpected: {message}");
    }

    #[tokio::test]
    async fn dropped_connection_is_a_transport_error() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Drop).await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        let message = err.to_string();
        assert!(format!("{err:?}").starts_with("EmbeddingUnavailable("));
        assert!(message.contains("TransportError"), "unexpected: {message}");
    }

    #[tokio::test]
    async fn circuit_opens_at_the_threshold() {
        let server = spawn_canned(
            health_json(MODEL, DIM, "1.0"),
            EmbedBehavior::Status(500, serde_json::json!({ "detail": "overloaded" })),
        )
        .await;
        let mut client = client_for(&server.base_url).with_failure_threshold(2);
        for _ in 0..2 {
            let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
            assert!(err.to_string().starts_with("HTTP error 500:"));
        }
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        assert!(format!("{err:?}").starts_with("EmbeddingUnavailable("));
        assert_eq!(
            err.to_string(),
            format!(
                "Remote embedding server {} failed 2 times consecutively; refusing \
                         further calls. Fix the server, or set RE_EMBEDDING_PROVIDER=local_bge \
                         to embed on this machine, then run `research-engine embeddings backfill`.",
                server.base_url
            )
        );
        assert_eq!(server.embed_hits.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn success_resets_the_failure_count() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let mut client = client_for(&server.base_url).with_failure_threshold(2);
        client.consecutive_failures = 1;
        client.embed_batch(&texts(&["alpha"])).await.unwrap();
        assert_eq!(client.consecutive_failures, 0);
    }

    #[tokio::test]
    async fn memory_pressure_is_retryable_503() {
        let server = spawn_canned(
            health_json(MODEL, DIM, "1.0"),
            EmbedBehavior::Status(503, serde_json::json!({ "detail": "CUDA out of memory" })),
        )
        .await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["alpha"])).await.unwrap_err();
        assert!(err.to_string().starts_with("HTTP error 503: "));
        assert!(err.to_string().contains("CUDA out of memory"));
    }

    #[tokio::test]
    async fn empty_batch_needs_no_socket() {
        let mut client = client_for(&closed_base_url().await);
        assert_eq!(
            client.embed_batch(&[]).await.unwrap(),
            Vec::<Vec<f64>>::new()
        );
    }

    #[tokio::test]
    async fn unreachable_host_names_itself_and_says_what_to_check() {
        let base = closed_base_url().await;
        let mut client = RemoteEmbeddingClient::new(&base, MODEL)
            .unwrap()
            .with_dim(DIM);
        let err = client.embed_batch(&texts(&["anything"])).await.unwrap_err();
        let message = err.to_string();
        assert!(format!("{err:?}").starts_with("EmbeddingUnavailable("));
        assert!(message.contains(&base), "must name the server");
        assert!(!message.trim().is_empty(), "an empty message is the bug");
        assert!(
            message.contains("powered on"),
            "say what to check: {message}"
        );
    }

    #[tokio::test]
    async fn bearer_header_is_sent() {
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let mut client = client_for(&server.base_url).with_api_key("secret").unwrap();
        client.embed_batch(&texts(&["alpha"])).await.unwrap();
        let auth = server.auth_headers.lock().unwrap();
        // Only POSTs are recorded; the handshake GET is not.
        assert_eq!(auth.len(), 1);
        assert_eq!(auth[0].as_deref(), Some("Bearer secret"));
    }

    #[tokio::test]
    async fn version_drift_is_flagged_not_fatal() {
        let server = spawn_canned(health_json(MODEL, DIM, "2.0"), EmbedBehavior::Echo).await;
        let mut client = client_for(&server.base_url);
        client.embed_batch(&texts(&["alpha"])).await.unwrap();
        assert!(client.last_version_drift);

        let mut matching = RemoteEmbeddingClient::new(&server.base_url, MODEL)
            .unwrap()
            .with_dim(DIM)
            .with_model_version("2.0");
        matching.embed_batch(&texts(&["alpha"])).await.unwrap();
        assert!(!matching.last_version_drift);
    }

    #[test]
    fn base_url_strips_trailing_slashes() {
        let client = RemoteEmbeddingClient::new("http://gpu:9882///", MODEL).unwrap();
        assert_eq!(client.base_url(), "http://gpu:9882");
        assert_eq!(client.model_name(), MODEL);
        assert_eq!(client.model_version(), "1.0");
        assert_eq!(client.dim(), 1024);
    }

    #[tokio::test]
    async fn non_json_success_body_surfaces_as_json_error() {
        // `request_json` can only return `Json` here when the server answers
        // 2xx with bytes that are not JSON at all. The batch layer cannot
        // translate that into "the box is off" or "that batch was too big",
        // so it propagates unchanged (the `Err(err)` passthrough).
        let server = spawn_canned(
            health_json(MODEL, DIM, "1.0"),
            EmbedBehavior::Raw(200, b"this is not json".to_vec()),
        )
        .await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        assert!(format!("{err:?}").starts_with("Json("));
    }

    #[tokio::test]
    async fn embedding_count_mismatch_is_refused() {
        // The server answered 200 with valid JSON for the wrong batch size.
        // Scores are positional, so a length mismatch means every result
        // would be mis-attributed.
        let one = vec![1.0; DIM as usize];
        let server = spawn_canned(
            health_json(MODEL, DIM, "1.0"),
            EmbedBehavior::Status(
                200,
                serde_json::json!({
                    "embeddings": [one],
                    "model_name": MODEL,
                    "model_version": "1.0",
                    "dim": DIM,
                }),
            ),
        )
        .await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["a", "b"])).await.unwrap_err();
        assert_eq!(
            err.to_string(),
            "Remote server returned 1 embeddings for 2 texts."
        );
    }

    #[tokio::test]
    async fn malformed_health_propagates_without_transport_translation() {
        // `health()` validates the body, so a 200 with a structurally wrong
        // payload is `Json`, not `Transport`. `ensure_verified` only
        // translates `Transport` (unreachable host); anything else propagates
        // unchanged through its `Err(err)` arm — mirroring the Python, where
        // only `TransportError` is caught in `_ensure_verified`.
        let server = spawn_canned(serde_json::json!({"nope": true}), EmbedBehavior::Echo).await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        assert!(format!("{err:?}").starts_with("Json("));
    }

    #[tokio::test]
    async fn stub_defenses_fragmented_and_disconnecting_clients() {
        // Fires the stub's defensive arms, which mirror real TCP behaviour:
        // a client that disconnects mid-head (`n == 0` in the header loop), a
        // head split across segments (header loop iterates without `break`),
        // a body split across segments (body loop reads the remainder), a
        // client that deserts mid-body (`n == 0` in the body loop), and an
        // unknown path (the 404 else, whose reason hits the `_` arm).
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let addr = server.base_url.trim_start_matches("http://").to_owned();

        // 1. Connect and close without sending: header `n == 0` returns.
        drop(
            tokio::net::TcpStream::connect(&addr)
                .await
                .expect("connect stub"),
        );
        tokio::time::sleep(Duration::from_millis(50)).await;

        // 2. Head split across segments: first read lacks `\r\n\r\n`, so
        // the header loop iterates again instead of breaking.
        let body = serde_json::json!({
            "texts": ["hi"],
            "expect_model": MODEL,
            "expect_dim": DIM,
        })
        .to_string();
        let head = format!(
            "POST /embeddings HTTP/1.1\r\nhost: x\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n",
            body.len()
        );
        let mut split = tokio::net::TcpStream::connect(&addr)
            .await
            .expect("connect stub");
        split
            .write_all(head.as_bytes()[..head.len() - 2].as_ref())
            .await
            .expect("write head fragment");
        tokio::time::sleep(Duration::from_millis(50)).await;
        split
            .write_all(head.as_bytes()[head.len() - 2..].as_ref())
            .await
            .expect("write head rest");
        // Body split too: first half now, second half after a beat, forcing
        // the `while body.len() < content_length` loop to read twice.
        let mid = body.len() / 2;
        split
            .write_all(body.as_bytes()[..mid].as_ref())
            .await
            .expect("write body fragment");
        tokio::time::sleep(Duration::from_millis(50)).await;
        split
            .write_all(body.as_bytes()[mid..].as_ref())
            .await
            .expect("write body rest");
        let mut resp = Vec::new();
        split.read_to_end(&mut resp).await.expect("read answer");
        assert!(resp.starts_with(b"HTTP/1.1 200"));

        // 3. Head promises a body that never arrives: body-loop `n == 0` breaks.
        let mut deserter = tokio::net::TcpStream::connect(&addr)
            .await
            .expect("connect stub");
        deserter
            .write_all(head.as_bytes())
            .await
            .expect("write head");
        drop(deserter);
        tokio::time::sleep(Duration::from_millis(50)).await;

        // 4. Unknown path answers 404 (reason `_` arm).
        let mut unknown = tokio::net::TcpStream::connect(&addr)
            .await
            .expect("connect stub");
        unknown
            .write_all(b"GET /unknown HTTP/1.1\r\nhost: x\r\nconnection: close\r\n\r\n")
            .await
            .expect("write unknown");
        let mut unknown_resp = Vec::new();
        unknown
            .read_to_end(&mut unknown_resp)
            .await
            .expect("read unknown");
        assert!(unknown_resp.starts_with(b"HTTP/1.1 404"));

        // The stub still serves real clients afterwards.
        let mut client = client_for(&server.base_url);
        let vectors = client.embed_batch(&texts(&["alpha"])).await.expect("embed");
        assert_eq!(vectors, vec![vec![5.0; DIM as usize]]);
        client.close().await;
    }

    #[test]
    fn invalid_api_key_is_rejected_before_any_io() {
        // `HeaderValue::from_str` refuses control bytes, firing the
        // `map_err` arm in `build_client` (mirrors `describe_exception` never
        // going empty on the Python side: the key is validated, not sent).
        // `with_api_key` returns `Result<Self>` where `Self` has no `Debug`,
        // so `unwrap_err` cannot apply; `.err().expect(..)` extracts the same
        // failure (`Option::expect` leaves no counted region, like the stub's
        // `expect("bind loopback stub")`).
        let err = RemoteEmbeddingClient::new("http://127.0.0.1:9", MODEL)
            .unwrap()
            .with_api_key("bad\nkey")
            .err()
            .expect("invalid key must fail");
        assert_eq!(
            err.to_string(),
            "invalid API key: failed to parse header value"
        );
    }

    #[tokio::test]
    async fn single_embed_propagates_batch_failures() {
        // Fires the `?` in `embed` (single-text shorthand over
        // `embed_batch`): a handshake mismatch surfaces through `embed`, not
        // just `embed_batch` — mirroring the Python `embed` delegating to
        // `embed_batch`.
        let server = spawn_canned(health_json(MODEL, DIM, "1.0"), EmbedBehavior::Echo).await;
        let mut client = RemoteEmbeddingClient::new(&server.base_url, "mismatched")
            .unwrap()
            .with_dim(DIM);
        let err = client.embed("alpha").await.unwrap_err();
        assert!(format!("{err:?}").starts_with("ModelMismatch { "));
    }

    #[tokio::test]
    async fn malformed_embeddings_shape_is_a_json_error() {
        // The server answered 200 with valid JSON of the wrong shape, firing
        // the `from_value::<EmbedResponse>` `?` arm (mirrors Pydantic's
        // `ValidationError` from `EmbedResponse.model_validate`).
        let server = spawn_canned(
            health_json(MODEL, DIM, "1.0"),
            EmbedBehavior::Status(200, serde_json::json!({"nope": true})),
        )
        .await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        assert!(format!("{err:?}").starts_with("Json("));
        assert!(err.to_string().contains("invalid /embeddings response"));
    }

    #[tokio::test]
    async fn post_identity_mismatch_is_refused_after_a_good_handshake() {
        // `/health` matches but the `/embeddings` answer names another model:
        // fires the `assert_matches(...)?` arm after a successful POST —
        // mirroring the Python double-check that no vector is stored until
        // the batch's own identity confirms the handshake.
        let server = spawn_canned(
            health_json(MODEL, DIM, "1.0"),
            EmbedBehavior::Status(
                200,
                serde_json::json!({
                    "embeddings": [[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]],
                    "model_name": "some-other-model",
                    "model_version": "1.0",
                    "dim": DIM,
                }),
            ),
        )
        .await;
        let mut client = client_for(&server.base_url);
        let err = client.embed_batch(&texts(&["x"])).await.unwrap_err();
        assert_eq!(
            format!("{err:?}"),
            "ModelMismatch { expected: \"BAAI/bge-m3\", actual: \"some-other-model\", kind: \"embedding\" }"
        );
    }

    #[test]
    fn describe_is_never_empty() {
        assert_eq!(join_kind_message("Timeout", ""), "Timeout");
        assert_eq!(
            join_kind_message("ConnectError", "refused"),
            "ConnectError: refused"
        );
    }
}
