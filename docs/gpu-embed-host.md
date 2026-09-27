# GPU embed host: idle unless bulk work needs it

The RTX 3090 in `john-super-server` is shared: Marginalia's embed server holds
13.4 GB resident when left running, against vidgen's Chatterbox TTS (~8 GB)
and the Breeze TTS 2 trial (~8–15 GB). Book ingestion — the main consumer of
bulk embeddings — is uncommon, so the card should sit at 0 MB from Marginalia
unless an ingest is running or ran within the last 15 minutes.

Three pieces do that. No manual babysitting.

## How it works

| Piece | Where | Behaviour |
|---|---|---|
| Ensure-started bracket | `adapters/inference/gpu_host.py`, called by `ingest`, `embeddings backfill`, `reindex chunks` | Before touching the database: `systemctl --user is-active marginalia-embed` (over `ssh <host>` when run from the laptop, locally on the server); if down, `start` it and poll `GET /health` until `warm=true` (timeout `RE_EMBED_START_TIMEOUT`, 300 s). Fails fast with one line otherwise. |
| Idle self-shutdown | `research-engine embed-server --idle-exit-after 900` | The server exits 0 after 900 s without an embedding or rerank request. `Restart=on-failure` stays stopped: a clean exit is not a failure. `/health` polls do not count as activity, or the bracket's own warm-wait would keep it alive. |
| Per-batch cache release | `adapters/embedding/server.py` | `torch.cuda.empty_cache()` after every batch, so a long run stops sitting on peak-batch memory while keeping model weights resident. |

The bracket deliberately never stops the unit, and there is no refcount for
overlapping ingests: shutdown is the server's idle-exit, which needs no
coordination. A client-side stop would need a lease living on the *host* to
survive two ingests from different machines (laptop over ssh, server local),
and a stale lease either leaks the server or stops it mid-run. Two overlapping
ingests just both proceed; the unit test pins that no path issues a stop.

Socket activation was considered and rejected: model warm-up is 30–60 s, past
the 30 s rerank timeout, so the first request after idle would fail — and the
idle-exit would still be needed anyway.

## One-time setup (on john-super-server)

```sh
# 1. Install the unit (content: deploy/marginalia-embed.service), then:
systemctl --user daemon-reload

# 2. Stop holding the card at boot. ASK BEFORE CHANGING THE LIVE UNIT —
# this is the one state change this design needs from a human.
systemctl --user disable marginalia-embed.service

# 3. Confirm linger stays on (already yes; user units need it):
loginctl show-user john  # Linger=yes
```

## Day to day

```sh
# Status / logs (on the server, or over ssh):
systemctl --user status marginalia-embed.service
journalctl --user -u marginalia-embed.service --since -30min

# Manual start/stop (normally unnecessary — bulk commands start, idle exits):
systemctl --user start marginalia-embed.service
systemctl --user stop marginalia-embed.service

# From the laptop, a normal ingest wakes the card itself:
research-engine ingest ~/books/new-book.epub
research-engine embeddings backfill
```

Verify with `nvidia-smi`: fresh boot shows no Marginalia process; after an
ingest the server is gone again within 15 minutes.

## Queries while the server is down

`research-engine search` still works, degraded, with no flags:

- Query embedding falls back to the laptop's own bge-m3 (loaded lazily, 2.3 GB
  per process — each CLI invocation pays the load once, then ~66 ms per query).
- Reranking is skipped and results are flagged `rerank_unavailable`.

Bulk work is different: ingestion and backfill fail loudly rather than
silently embedding on the laptop, which would turn hours on the 3090 into days
and scatter incomparable vectors. If the bracket cannot start or warm the
server, the command stops before opening the database.

Tradeoff to know: if you query from the laptop constantly, every invocation
reloads the 2.3 GB fallback model while the server is down. Frequent queries
argue for a longer `--idle-exit-after` (e.g. 3600) so the card stays warm
between searches; rare queries argue for the 900 s default. The server never
needs to be manually kept up for queries either way — the next bulk run wakes
it.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `RE_INFERENCE_BASE_URL` | unset | `http://john-super-server:9882`. Unset means fully local. |
| `RE_EMBEDDING_PROVIDER` | `local_bge` | `auto` = host when it answers, local fallback for queries. |
| `RE_EMBED_MANAGE_GPU` | `auto` | `auto` starts the unit only when bulk embedding is remote; `always` forces it when a host is set; `never` leaves systemd alone. |
| `RE_EMBED_SSH_TARGET` | URL hostname | ssh target when the URL does not name the box itself. |
| `RE_EMBED_START_TIMEOUT` | `300.0` | Seconds to wait for `warm=true` after start. |

## Troubleshooting

- `Could not start marginalia-embed.service on <host>` — ssh needs
  key auth (`BatchMode=yes`, no password prompts) and Tailscale up. The exact
  command is in the error; run it by hand to see why.
- `did not report warm=true within Ns` — the unit started but the model did
  not load: `journalctl --user -u marginalia-embed.service` (OOM, missing
  weights, wrong venv path in `ExecStart`).
- Ingest fails immediately with `Stopped: ...` — by design: it failed before
  the database, nothing is half-written. Fix the server, re-run.
- `embeddings backfill --dry-run` never wakes the server (it only counts
  candidates in Postgres). `reindex chunks --dry-run` does wake it — the dry
  run still embeds before rolling back.
