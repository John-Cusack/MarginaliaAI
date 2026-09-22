//! Postgres repository integration tests for `marginalia-ret`.
//!
//! What this covers, case-for-case at the repo level: `PgTx`
//! begin/commit/rollback-visibility, `db_err` mapping (bad-port pool),
//! `format_vector_py`/`format_float_py` edges through real vector traffic,
//! and every `PgDocumentRepo` / `PgDocumentTextRepo` / `PgPassageRepo` /
//! `PgSourceSpanRepo` / `PgDocumentNodeRepo` / `PgLemmaLookup` method
//! including error and edge paths (validation refusals, not-found, empty
//! inputs, stale spans, orphan-free reindex shapes, backfill report queries,
//! lemma cap/zero/partials/qere, vector/keyword/candidate/covering/context/
//! embed/fts round-trips, bulk updates, tree operations).
//!
//! Python mirrors (`tests/integration/`): `test_spans` (resolve idempotence
//! and convergence, canonical slice, best overlap, stale on re-parse,
//! delete-restrict), `test_search_windows` (batched spans incl. the outer-join
//! hole, negative-start clamp, past-end tail, ancestors-many agreement),
//! `test_find_lemma` (survey counts, citable occurrences, versification hop,
//! map-loaded guard, homographs, narrowing, zero/collision),
//! `test_versification` (verse_map row count, partial shapes, the five
//! hand-surveyed mappings, 27 mapped books, Joel.4.1 — the `VERSE_NODES`
//! edition-join halves live outside this crate's repos and stay Python-side),
//! `test_reindex` (true offsets, searchable-after-reindex, structure rebuild,
//! bare root — the reindex *service* is orchestration over these repo calls),
//! `test_text_backfill` (missing-list candidacy, put-on-plan, dry-run silence
//! — likewise repo-level), `test_keyword_search_language` (per-language
//! stemming, lang-config routing on reindex, `simple` fallback, GIN shape),
//! `test_document_nodes` (tree round-trip, outline, subtree, ancestors,
//! deepest span, bare root, cascade delete, node stamping, late structure,
//! node reads, rebuild SET NULL), `test_document_texts` (round-trip,
//! re-parse replace, offsets, missing list, cascade delete, span slicing).
//!
//! # Scratch lifecycle
//!
//! One scratch database per test-binary run. `CREATE DATABASE ... TEMPLATE
//! research_engine` is **banned**: on 2026-09-20 it crashed the shared dev
//! server (3.7 GB template, WAL burst, postmaster recovery — dev data was
//! unharmed but the run was lost), so nobody retries it. Instead the setup
//! replays a schema-only `pg_dump` (carries `vector`/`pg_trgm`/`ltree` plus
//! every table) and `COPY`s only the reference rows the lemma tests read:
//! all of `core.verse_map` (1978 rows), all of `core.edition_books`, every
//! `core.words` row for the surveyed Strong's numbers, and those words'
//! parent documents (with `edition_id` nulled — the one FK pointing outside
//! the copied set, unread by every repo here). All writes land in scratch;
//! the dev database is read (dump/COPY source) but never written.
//!
use chrono::{TimeZone, Utc};
use marginalia_ret::filters::{ExtensionClause, Param};
use marginalia_ret::repos::{
    PgDocumentNodeRepo, PgDocumentRepo, PgDocumentTextRepo, PgLemmaLookup, PgPassageRepo,
    PgSourceSpanRepo, PgTx,
};
use marginalia_ret::words::{LemmaQuery, Mapping};
use marginalia_types::documents::{Document, DocumentDraft, DocumentFilter};
use marginalia_types::errors::Error;
use marginalia_types::nodes::{DocumentNode, DocumentNodeDraft};
use marginalia_types::passages::Passage;
use marginalia_types::ports::{DocumentRepo, DocumentTextRepo, PassageRepo, SourceSpanRepo};
use marginalia_types::sdk::PassageDraft;
use marginalia_types::spans::SourceSpan;
use serde_json::{Map, Value};
use sqlx::PgPool;
use std::collections::HashMap;
use std::process::Command;
use uuid::Uuid;

// ---------------------------------------------------------------------------
// Scratch setup
// ---------------------------------------------------------------------------

/// Strong's numbers under survey: the two Python totals, the cap-buster, the
const SEED_STRONGS: &str = "4941','6666','853','2416','2151";

const GERMAN: &str = "Die Häuser in der Altstadt waren sehr alt.";
const ENGLISH: &str = "The scholars were running experiments in the laboratory.";

fn dev_url() -> String {
    std::env::var("DATABASE_URL")
        .or_else(|_| std::env::var("TEST_PG_URL"))
        .unwrap_or_else(|_| {
            "postgresql://re_dev:re_dev_pass@localhost:5435/research_engine".to_string()
        })
}

/// The maintenance database on the same host: creating databases from the
/// reference database itself would both write to it and fight its sessions.
fn admin_url(dev: &str) -> String {
    match dev.rfind('/') {
        Some(idx) => format!("{}/postgres", &dev[..idx]),
        None => dev.to_string(),
    }
}

struct Scratch {
    name: String,
    admin: String,
    url: String,
    pool: PgPool,
}

/// Run one `psql | psql` COPY pipeline. `COPY ... TO STDOUT` on dev feeds
/// `COPY ... FROM STDIN` on scratch, so `bytea`/`json`/`timestamptz` cross in
/// Postgres text format with no client decoding involved.
fn copy_table(dev: &str, scratch: &str, out_sql: &str, in_sql: &str) {
    let script =
        format!("psql '{dev}' -X -q -c \"{out_sql}\" | psql '{scratch}' -X -q -c \"{in_sql}\"");
    let status = Command::new("sh")
        .arg("-c")
        .arg(&script)
        .env("PGPASSWORD", "re_dev_pass")
        .env("PGCONNECT_TIMEOUT", "30")
        .status()
        .expect("sh must run the COPY pipeline");
    assert!(status.success(), "COPY pipeline failed: {out_sql}");
}

async fn create_scratch() -> Scratch {
    let dev = dev_url();
    let admin = admin_url(&dev);
    let name = format!("ret_scratch_{}", std::process::id());
    let scratch_url = match dev.rfind('/') {
        Some(idx) => format!("{}/{name}", &dev[..idx]),
        None => panic!("dev URL has no database path: {dev}"),
    };

    // Drop our own stale database from a crashed run, and any other idle
    // `ret_scratch_*` left behind: databases with live backends are skipped,
    // never killed.
    let admin_pool = PgPool::connect_lazy(&admin).expect("lazy admin pool");
    let stale: Vec<String> = sqlx::query_scalar(
        "SELECT datname FROM pg_database WHERE datname LIKE 'ret_scratch\\_%' ESCAPE '\\'",
    )
    .fetch_all(&admin_pool)
    .await
    .expect("list stale scratch databases");
    for db in stale {
        let backends: i64 =
            sqlx::query_scalar("SELECT count(*) FROM pg_stat_activity WHERE datname = $1")
                .bind(&db)
                .fetch_one(&admin_pool)
                .await
                .expect("count backends");
        if backends == 0 {
            sqlx::query(&format!("DROP DATABASE IF EXISTS \"{db}\""))
                .execute(&admin_pool)
                .await
                .expect("drop idle stale scratch");
        }
    }

    sqlx::query(&format!("CREATE DATABASE \"{name}\""))
        .execute(&admin_pool)
        .await
        .expect("create scratch database");

    // Schema replay: schema-only dump carries extensions, tables, indexes,
    // constraints. No data, so no 3.7 GB copy and no TEMPLATE crash.
    let dump = Command::new("pg_dump")
        .arg(&dev)
        .arg("-s")
        .env("PGPASSWORD", "re_dev_pass")
        .output()
        .expect("pg_dump must run");
    assert!(dump.status.success(), "schema-only pg_dump failed");
    {
        use std::io::Write as _;
        let mut restore = Command::new("psql")
            .args([&scratch_url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"])
            .env("PGPASSWORD", "re_dev_pass")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .expect("psql restore must spawn");
        restore
            .stdin
            .as_mut()
            .expect("piped stdin")
            .write_all(&dump.stdout)
            .expect("feed schema to psql");
        let restore = restore.wait_with_output().expect("psql restore must run");
        assert!(
            restore.status.success(),
            "schema restore failed: {}",
            String::from_utf8_lossy(&restore.stderr)
        );
    }

    // Reference rows the lemma tests read.
    copy_table(
        &dev,
        &scratch_url,
        "COPY core.edition_books TO STDOUT",
        "COPY core.edition_books FROM STDIN",
    );
    copy_table(
        &dev,
        &scratch_url,
        "COPY core.verse_map TO STDOUT",
        "COPY core.verse_map FROM STDIN",
    );
    copy_table(
        &dev,
        &scratch_url,
        &format!(
            "COPY (SELECT id, title, document_type, language, source, content_hash, \
             parser, parser_version, ingested_at, created_date_start, created_date_end, \
             created_precision, metadata, NULL::uuid AS edition_id FROM core.documents d \
             WHERE d.id IN (SELECT DISTINCT document_id FROM core.words \
             WHERE language = 'he' AND strong IN ('{SEED_STRONGS}'))) TO STDOUT"
        ),
        "COPY core.documents (id, title, document_type, language, source, content_hash, \
         parser, parser_version, ingested_at, created_date_start, created_date_end, \
         created_precision, metadata, edition_id) FROM STDIN",
    );
    copy_table(
        &dev,
        &scratch_url,
        &format!(
            "COPY (SELECT id, document_id, position, char_start, char_end, surface, lemma, \
             strong, homograph, prefixes, morph, language, ref, from_qere FROM core.words \
             WHERE language = 'he' AND strong IN ('{SEED_STRONGS}')) TO STDOUT"
        ),
        "COPY core.words (id, document_id, position, char_start, char_end, surface, lemma, \
         strong, homograph, prefixes, morph, language, ref, from_qere) FROM STDIN",
    );

    let pool = PgPool::connect(&scratch_url)
        .await
        .expect("connect to scratch");
    sqlx::query("SELECT setval('core.words_id_seq', (SELECT max(id) FROM core.words))")
        .execute(&pool)
        .await
        .expect("advance words sequence");
    sqlx::query("SELECT setval('core.verse_map_id_seq', (SELECT max(id) FROM core.verse_map))")
        .execute(&pool)
        .await
        .expect("advance verse_map sequence");

    Scratch {
        name,
        admin,
        url: scratch_url,
        pool,
    }
}

async fn drop_scratch(scratch: Scratch) {
    let name = scratch.name.clone();
    scratch.pool.close().await;
    let admin_pool = PgPool::connect_lazy(&scratch.admin).expect("lazy admin pool");
    sqlx::query(&format!("DROP DATABASE IF EXISTS \"{name}\""))
        .execute(&admin_pool)
        .await
        .expect("drop scratch database");
    println!("dropped scratch database {name}");
    admin_pool.close().await;
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/// All fixture texts are ASCII, so byte offsets and char offsets agree and
/// expected slices can use plain Rust ranges (Unicode slicing would panic on
/// non-boundaries; the one multibyte case below slices by `chars()`).
const DOC_TEXT: &str = "The archive holds letters.\n\n\
    Each letter carries a date. Some dates are approximate. \
    Others are exact, recorded by the clerk who filed them.\n\n\
    A final paragraph closes the folder.";

fn doc_draft(source: &str, hash_byte: u8) -> DocumentDraft {
    DocumentDraft {
        title: Some("Fixture".to_string()),
        document_type: "generic".to_string(),
        language: Some("en".to_string()),
        source: source.to_string(),
        content_hash: vec![hash_byte; 32],
        parser: "test".to_string(),
        parser_version: "1.0".to_string(),
        created_date_start: None,
        created_date_end: None,
        created_precision: None,
        edition_id: None,
        metadata: Map::new(),
    }
}

async fn insert_doc(pool: &PgPool, draft: DocumentDraft) -> Document {
    let repo = PgDocumentRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let doc = repo.insert(&mut tx, draft).await.expect("insert");
    tx.commit().await.expect("commit");
    doc
}

async fn put_text(pool: &PgPool, doc: Uuid, text: &str, parser: &str, version: &str) {
    let repo = PgDocumentTextRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    repo.put(&mut tx, doc, text, parser, version)
        .await
        .expect("put text");
    tx.commit().await.expect("commit");
}

fn passage_draft(position: i64, text: &str) -> PassageDraft {
    let len = text.chars().count() as i64;
    PassageDraft {
        position,
        char_start: 0,
        char_end: len,
        locator: Map::new(),
        text: text.to_string(),
        token_count: Some(len),
        chunker: "test".to_string(),
        chunker_version: "1.0".to_string(),
        metadata: Map::new(),
        node_id: None,
    }
}

async fn insert_passages(pool: &PgPool, doc: Uuid, drafts: Vec<PassageDraft>) -> Vec<Passage> {
    let repo = PgPassageRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let out = repo
        .insert_many(&mut tx, doc, drafts)
        .await
        .expect("insert passages");
    tx.commit().await.expect("commit");
    out
}

/// The two-chapter book from `test_document_nodes`: enough depth that a
/// subtree query is not a child query. Parent spans are widened to enclose
/// their descendants, exactly as `build_node_tree` emits.
fn book_drafts(title: &str) -> Vec<DocumentNodeDraft> {
    let draft = |path: &str,
                 parent: Option<&str>,
                 depth: i64,
                 position: i64,
                 node_type: &str,
                 title: Option<&str>,
                 start: i64,
                 end: i64| {
        DocumentNodeDraft {
            path: path.to_string(),
            parent_path: parent.map(str::to_string),
            depth,
            position,
            node_type: node_type.to_string(),
            title: title.map(str::to_string),
            char_start: start,
            char_end: end,
            metadata: Map::new(),
        }
    };
    vec![
        draft("r", None, 0, 0, "document", Some(title), 0, 500),
        draft(
            "r.c1",
            Some("r"),
            1,
            0,
            "section",
            Some("Chapter One"),
            0,
            100,
        ),
        draft(
            "r.c2",
            Some("r"),
            1,
            1,
            "section",
            Some("Chapter Two"),
            100,
            500,
        ),
        draft(
            "r.c2.s1",
            Some("r.c2"),
            2,
            0,
            "section",
            Some("Two, First"),
            200,
            300,
        ),
        draft(
            "r.c2.s2",
            Some("r.c2"),
            2,
            1,
            "section",
            Some("Two, Second"),
            300,
            500,
        ),
        draft(
            "r.c2.s2.a",
            Some("r.c2.s2"),
            3,
            0,
            "section",
            Some("Two, Second, a"),
            400,
            500,
        ),
    ]
}

async fn insert_tree(pool: &PgPool, doc: Uuid, drafts: &[DocumentNodeDraft]) -> Vec<DocumentNode> {
    let repo = PgDocumentNodeRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let out = repo
        .insert_many(&mut tx, doc, drafts)
        .await
        .expect("insert tree");
    tx.commit().await.expect("commit");
    out
}

/// Delete what a scenario created: spans and mentions first (both RESTRICT
/// document deletion), then the document itself (cascades to texts, passages,
/// nodes, embeddings, FTS, words).
async fn cleanup_doc(pool: &PgPool, doc: Uuid) {
    sqlx::query("DELETE FROM evidence.source_spans WHERE document_id = $1")
        .bind(doc)
        .execute(pool)
        .await
        .expect("delete spans");
    sqlx::query(
        "DELETE FROM core.mentions WHERE passage_id IN \
         (SELECT id FROM core.passages WHERE document_id = $1)",
    )
    .bind(doc)
    .execute(pool)
    .await
    .expect("delete mentions");
    PgDocumentRepo::new(pool.clone())
        .delete(doc)
        .await
        .expect("delete document");
}

async fn cleanup_entities(pool: &PgPool, ids: &[Uuid]) {
    sqlx::query("DELETE FROM core.mentions WHERE entity_id = ANY($1)")
        .bind(ids)
        .execute(pool)
        .await
        .expect("delete entity mentions");
    sqlx::query("DELETE FROM core.entity_aliases WHERE entity_id = ANY($1)")
        .bind(ids)
        .execute(pool)
        .await
        .expect("delete aliases");
    sqlx::query("DELETE FROM core.entities WHERE id = ANY($1)")
        .bind(ids)
        .execute(pool)
        .await
        .expect("delete entities");
}

fn error_kind(err: &Error) -> &'static str {
    match err {
        Error::Validation(_) => "validation",
        Error::Storage(_) => "storage",
        Error::NotFound { .. } => "not_found",
        Error::UnsupportedFilter(_) => "unsupported",
        Error::UnknownFilterExtension(_) => "unknown_extension",
        Error::Plugin(_) => "plugin",
        _ => "other",
    }
}

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

/// `PgTx` commit persists, rollback is visible, and transport failures map to
/// `Error::Storage` via `db_err`.
async fn scn_transactions(pool: &PgPool) {
    // Commit path: the row survives.
    let committed = {
        let repo = PgDocumentRepo::new(pool.clone());
        let mut tx = PgTx::begin(pool).await.expect("begin");
        let doc = repo
            .insert(&mut tx, doc_draft("tx-commit", 0xC0))
            .await
            .expect("insert in tx");
        tx.commit().await.expect("commit");
        doc
    };
    assert!(
        PgDocumentRepo::new(pool.clone())
            .get(committed.id)
            .await
            .expect("get")
            .is_some(),
        "committed insert must be visible"
    );

    // Rollback path: the row never lands.
    let rolled_back = {
        let repo = PgDocumentRepo::new(pool.clone());
        let mut tx = PgTx::begin(pool).await.expect("begin");
        let doc = repo
            .insert(&mut tx, doc_draft("tx-rollback", 0xC1))
            .await
            .expect("insert in tx");
        let id = doc.id;
        tx.rollback().await.expect("rollback");
        id
    };
    assert!(
        PgDocumentRepo::new(pool.clone())
            .get(rolled_back)
            .await
            .expect("get")
            .is_none(),
        "rolled-back insert must be invisible"
    );

    // Transport failure maps through `db_err` to `Storage`, never a panic.
    let bad = PgPool::connect_lazy("postgresql://127.0.0.1:1/no_such_db").expect("lazy");
    let err = match PgTx::begin(&bad).await {
        Ok(_) => panic!("bad port must fail"),
        Err(err) => err,
    };
    assert_eq!(
        error_kind(&err),
        "storage",
        "db_err maps to Storage: {err:?}"
    );

    cleanup_doc(pool, committed.id).await;
}

/// Document CRUD: insert re-reads server defaults, point lookups hit and
/// miss, bulk reads keep input order and skip the missing, hash lookup, merge
/// update, counts, delete.
async fn scn_documents_crud(pool: &PgPool) {
    let repo = PgDocumentRepo::new(pool.clone());
    let first = insert_doc(pool, doc_draft("crud-1", 0xD1)).await;
    let second = insert_doc(pool, doc_draft("crud-2", 0xD2)).await;
    assert!(first.title.as_deref() == Some("Fixture"));

    let got = repo.get(first.id).await.expect("get").expect("present");
    assert_eq!(got, first, "get returns the inserted row");
    assert!(
        repo.get(Uuid::now_v7()).await.expect("get").is_none(),
        "unknown id reads None, not an error"
    );

    assert!(
        repo.get_many(&[]).await.expect("empty").is_empty(),
        "empty input answers without touching the database"
    );
    let many = repo
        .get_many(&[second.id, Uuid::now_v7(), first.id])
        .await
        .expect("get_many");
    assert_eq!(
        many.iter().map(|d| d.id).collect::<Vec<_>>(),
        vec![second.id, first.id],
        "input order kept, missing ids omitted"
    );

    let by_hash = repo
        .find_by_hash(&[0xD1u8; 32], "crud-1")
        .await
        .expect("find_by_hash")
        .expect("hash hit");
    assert_eq!(by_hash.id, first.id);
    assert!(repo
        .find_by_hash(&[0xFFu8; 32], "crud-1")
        .await
        .expect("hash miss")
        .is_none());
    // `find_by_edition_id` reads inside the caller's transaction: the
    // caller holds the edition row locked, so the pool must not serve it.
    let edition = Uuid::now_v7();
    let mut edition_draft = doc_draft("crud-ed", 0xD3);
    edition_draft.edition_id = Some(edition);
    let ed_doc = insert_doc(pool, edition_draft).await;
    assert_eq!(
        ed_doc.edition_id,
        Some(edition),
        "insert carries the edition"
    );
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let by_edition = repo
        .find_by_edition_id(&mut tx, edition)
        .await
        .expect("find_by_edition_id")
        .expect("edition hit");
    assert_eq!(by_edition.id, ed_doc.id);
    assert!(repo
        .find_by_edition_id(&mut tx, Uuid::now_v7())
        .await
        .expect("edition miss")
        .is_none());
    tx.commit().await.expect("commit");
    cleanup_doc(pool, ed_doc.id).await;

    let mut patch = Map::new();
    patch.insert("shelf".to_string(), Value::from("a3"));
    let updated = repo
        .update_metadata(first.id, patch)
        .await
        .expect("update_metadata");
    assert_eq!(
        updated.metadata.get("shelf"),
        Some(&Value::from("a3")),
        "patch merges into stored metadata"
    );

    let before = repo.count(None).await.expect("count all");
    assert!(before >= 2);
    let filtered = repo
        .count(Some(&DocumentFilter {
            source_pattern: Some("crud-".to_string()),
            ..DocumentFilter::default()
        }))
        .await
        .expect("count filtered");
    assert!(filtered >= 2 && filtered <= before);

    repo.delete(first.id).await.expect("delete");
    assert!(repo.get(first.id).await.expect("get").is_none());
    cleanup_doc(pool, second.id).await;
}

/// `iter_by_filter` exercises every `filtered_select` branch; `metadata`
/// contributes no clause; scalar stored metadata still decodes to `{}`.
async fn scn_documents_filters(pool: &PgPool) {
    let repo = PgDocumentRepo::new(pool.clone());
    let start = Utc.with_ymd_and_hms(1820, 1, 1, 0, 0, 0).unwrap();
    let end = Utc.with_ymd_and_hms(1830, 1, 1, 0, 0, 0).unwrap();
    let mut in_range = doc_draft("filter-book", 0xE1);
    in_range.document_type = "book".to_string();
    in_range.language = Some("en".to_string());
    in_range.created_date_start = Some(start);
    in_range.created_date_end = Some(end);
    let in_range = insert_doc(pool, in_range).await;
    let mut out_of_range = doc_draft("filter-article", 0xE2);
    out_of_range.document_type = "article".to_string();
    out_of_range.language = Some("de".to_string());
    out_of_range.created_date_start = Some(Utc.with_ymd_and_hms(1901, 1, 1, 0, 0, 0).unwrap());
    let out_of_range = insert_doc(pool, out_of_range).await;

    // A row whose stored metadata is a scalar, not an object: the mapping
    // still answers `{}` rather than failing to decode.
    let scalar_meta: Uuid = sqlx::query_scalar(
        "INSERT INTO core.documents (id, title, document_type, language, source, \
         content_hash, parser, parser_version, metadata) VALUES ($1, 'S', 'book', 'en', \
         'filter-scalar', $2, 'test', '1.0', '\"just a string\"') RETURNING id",
    )
    .bind(Uuid::now_v7())
    .bind(vec![0xE3u8; 32])
    .fetch_one(pool)
    .await
    .expect("raw scalar-metadata insert");
    let decoded = repo.get(scalar_meta).await.expect("get").expect("present");
    assert!(
        decoded.metadata.is_empty(),
        "non-object metadata decodes to empty"
    );

    let hits = repo
        .iter_by_filter(&DocumentFilter {
            document_types: Some(vec!["book".to_string()]),
            date_start: Some(start),
            date_end: Some(end),
            language: Some("en".to_string()),
            source_pattern: Some("filter-".to_string()),
            ..DocumentFilter::default()
        })
        .await
        .expect("filtered");
    let ids: Vec<Uuid> = hits.iter().map(|d| d.id).collect();
    assert!(ids.contains(&in_range.id), "in-range book matches");
    assert!(!ids.contains(&out_of_range.id), "out-of-range row excluded");

    // `metadata` contributes no clause: the same filter with and without it
    // answers identically.
    let mut meta_filter = DocumentFilter {
        document_types: Some(vec!["book".to_string()]),
        ..DocumentFilter::default()
    };
    let plain = repo.iter_by_filter(&meta_filter).await.expect("plain");
    let mut meta_map = Map::new();
    meta_map.insert("k".to_string(), Value::from("v"));
    meta_filter.metadata = Some(meta_map);
    let with_meta = repo.iter_by_filter(&meta_filter).await.expect("with meta");
    assert_eq!(
        plain.iter().map(|d| d.id).collect::<Vec<_>>(),
        with_meta.iter().map(|d| d.id).collect::<Vec<_>>(),
        "metadata must not change document selection"
    );

    // `find_by_metadata` reads the JSON containment the pack key relies on.
    let mut meta_patch = Map::new();
    meta_patch.insert("resource_id".to_string(), Value::from("logos-7"));
    repo.update_metadata(in_range.id, meta_patch)
        .await
        .expect("tag resource");
    let found = repo
        .find_by_metadata("resource_id", "logos-7")
        .await
        .expect("find_by_metadata");
    assert_eq!(
        found.iter().map(|d| d.id).collect::<Vec<_>>(),
        vec![in_range.id]
    );
    assert!(repo
        .find_by_metadata("resource_id", "absent")
        .await
        .expect("miss")
        .is_empty());

    // A scalar stored row merges from empty rather than failing the
    // read-modify-write, and the empty-but-set filter fields contribute no
    // clause while date bounds bind as timestamptz.
    let mut scalar_patch = Map::new();
    scalar_patch.insert("recovered".to_string(), Value::from(true));
    let merged = repo
        .update_metadata(scalar_meta, scalar_patch)
        .await
        .expect("scalar merge");
    assert_eq!(merged.metadata.len(), 1);
    let dated = repo
        .count(Some(&DocumentFilter {
            date_start: Some(start),
            date_end: Some(end),
            ..DocumentFilter::default()
        }))
        .await
        .expect("dated count");
    assert!(dated >= 1);
    let unfiltered = repo
        .iter_by_filter(&DocumentFilter {
            document_types: Some(Vec::new()),
            language: Some(String::new()),
            source_pattern: Some(String::new()),
            ..DocumentFilter::default()
        })
        .await
        .expect("empty fields");
    assert!(unfiltered.iter().any(|d| d.id == in_range.id));
    for id in [in_range.id, out_of_range.id, scalar_meta] {
        cleanup_doc(pool, id).await;
    }
}

/// Not-found updates refuse; a cited document cannot be deleted (RESTRICT
/// maps to `Storage`); removing the span first lets the document go.
async fn scn_documents_not_found_and_restrict(pool: &PgPool) {
    let repo = PgDocumentRepo::new(pool.clone());
    let err = repo
        .update_metadata(Uuid::now_v7(), Map::new())
        .await
        .expect_err("missing document must refuse");
    match err {
        Error::NotFound { kind, .. } => assert_eq!(kind, "document"),
        other => panic!("expected NotFound, got {other:?}"),
    }

    // A document whose stored metadata is NULL-adjacent (scalar here)
    // merges from empty rather than failing the read-modify-write.
    let doc = insert_doc(pool, doc_draft("restrict-1", 0xE4)).await;
    put_text(pool, doc.id, DOC_TEXT, "test", "1.0").await;
    let saved = insert_passages(pool, doc.id, vec![passage_draft(0, "alpha beta")]).await;
    let span_repo = PgSourceSpanRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let span = span_repo
        .resolve(&mut tx, doc.id, 0, 5)
        .await
        .expect("resolve span");
    tx.commit().await.expect("commit");
    assert_eq!(span.passage_id, Some(saved[0].id));

    let err = repo
        .delete(doc.id)
        .await
        .expect_err("cited delete must fail");
    assert_eq!(error_kind(&err), "storage", "FK violation maps to Storage");
    cleanup_doc(pool, doc.id).await;
}

/// Canonical text: round-trip with lossy fold, re-parse replace, offsets that
/// still address the text, the missing list (bounded and not), cascade
/// delete, slice-exact `get_span`, and the trigram needle search with
/// metacharacter escapes.
async fn scn_texts_round_trip(pool: &PgPool) {
    let repo = PgDocumentTextRepo::new(pool.clone());
    let doc = insert_doc(pool, doc_draft("texts-1", 0xF1)).await;
    put_text(pool, doc.id, DOC_TEXT, "docling", "2.1.0").await;

    let stored = repo.get(doc.id).await.expect("get").expect("present");
    assert_eq!(stored.text, DOC_TEXT);
    assert_eq!(stored.parser, "docling");
    assert_eq!(stored.parser_version, "2.1.0");
    assert!(!stored.normalization_version.is_empty());
    assert!(
        !stored.normalized_text.contains("\n\n"),
        "normalization folds paragraph breaks"
    );
    assert_eq!(
        repo.get_text(doc.id).await.expect("text"),
        Some(DOC_TEXT.to_string())
    );
    assert!(repo.get(Uuid::now_v7()).await.expect("miss").is_none());
    assert!(repo.get_text(Uuid::now_v7()).await.expect("miss").is_none());

    let (raw, norm) = repo
        .lengths(doc.id)
        .await
        .expect("lengths")
        .expect("present");
    assert_eq!(raw, DOC_TEXT.chars().count() as i64);
    assert_eq!(norm, stored.normalized_text.chars().count() as i64);
    assert!(repo.lengths(Uuid::now_v7()).await.expect("miss").is_none());

    let versions = repo
        .parser_versions(&[doc.id, Uuid::now_v7()])
        .await
        .expect("versions");
    assert_eq!(versions.get(&doc.id).map(String::as_str), Some("2.1.0"));
    assert_eq!(versions.len(), 1, "documents without text stay absent");
    assert!(repo.parser_versions(&[]).await.expect("empty").is_empty());

    // Re-parsing replaces rather than conflicting.
    put_text(pool, doc.id, "Re-parsed, differently.", "docling", "2.2.0").await;
    let stored = repo.get(doc.id).await.expect("get").expect("present");
    assert_eq!(stored.text, "Re-parsed, differently.");
    assert_eq!(stored.parser_version, "2.2.0");
    put_text(pool, doc.id, DOC_TEXT, "docling", "2.1.0").await;

    // Slices agree with the client-side slice at every edge, because passages
    // and nodes are addressed by exactly these offsets.
    let end = DOC_TEXT.chars().count() as i64;
    for (start, stop) in [(0, end), (0, 1), (end - 1, end), (4, 17), (10, 10)] {
        let expected: String = DOC_TEXT
            .chars()
            .skip(start as usize)
            .take((stop - start) as usize)
            .collect();
        assert_eq!(
            repo.get_span(doc.id, start, stop).await.expect("span"),
            Some(expected),
            "get_span({start}, {stop}) disagrees with the slice"
        );
    }
    let no_text = insert_doc(pool, doc_draft("texts-2", 0xF2)).await;
    assert!(repo
        .get_span(no_text.id, 0, 10)
        .await
        .expect("miss")
        .is_none());
    assert!(
        repo.get_span(no_text.id, 0, 0)
            .await
            .expect("miss")
            .is_none(),
        "missing text answers None even for an empty span"
    );

    // Batched spans agree one-at-a-time; the outer join leaves a hole rather
    // than shifting; negative starts clamp; past-end tails return what exists.
    let requests = vec![(doc.id, 0, 20), (doc.id, 40, 90), (doc.id, 10, 11)];
    let batched = repo.get_spans(&requests).await.expect("batched");
    let mut one_by_one = Vec::new();
    for (id, s, e) in &requests {
        one_by_one.push(repo.get_span(*id, *s, *e).await.expect("single"));
    }
    assert_eq!(batched, one_by_one);
    assert!(repo.get_spans(&[]).await.expect("empty").is_empty());
    let holed = repo
        .get_spans(&[(doc.id, 0, 10), (Uuid::now_v7(), 0, 10), (doc.id, 10, 20)])
        .await
        .expect("hole");
    assert!(holed[1].is_none());
    let slice = |s: i64, e: i64| {
        DOC_TEXT
            .chars()
            .skip(s as usize)
            .take((e - s) as usize)
            .collect::<String>()
    };
    assert_eq!(holed[0], Some(slice(0, 10)));
    assert_eq!(holed[2], Some(slice(10, 20)));
    assert_eq!(
        repo.get_spans(&[(doc.id, -50, 30)]).await.expect("clamp"),
        vec![Some(slice(0, 30))],
        "negative starts clamp instead of eating the length"
    );
    assert_eq!(
        repo.get_spans(&[(doc.id, end - 10, end + 5_000)])
            .await
            .expect("tail"),
        vec![Some(slice(end - 10, end))],
    );

    // `strpos` offsets: hit, miss, and the empty needle that answers None.
    assert_eq!(
        repo.find_raw(doc.id, "archive").await.expect("raw"),
        Some(4)
    );
    assert!(repo
        .find_raw(doc.id, "no such phrase")
        .await
        .expect("miss")
        .is_none());
    assert!(repo.find_raw(doc.id, "").await.expect("empty").is_none());
    assert!(repo
        .find_normalized(doc.id, "archive")
        .await
        .expect("norm")
        .is_some());
    assert!(repo
        .find_normalized(doc.id, "")
        .await
        .expect("empty")
        .is_none());
    assert!(
        repo.find_raw(Uuid::now_v7(), "archive")
            .await
            .expect("miss")
            .is_none(),
        "no row means not found, not zero"
    );

    // Trigram search with LIKE metacharacters in the needle.
    let tricky = insert_doc(pool, doc_draft("texts-3", 0xF3)).await;
    put_text(
        pool,
        tricky.id,
        "Coverage is 100%_of it \\ all.",
        "test",
        "1.0",
    )
    .await;
    assert!(repo
        .find_documents_containing("", 10)
        .await
        .expect("empty")
        .is_empty());
    for needle in ["100%_of", "100%", "%_of", "\\ all", "Coverage"] {
        let hits = repo
            .find_documents_containing(needle, 10)
            .await
            .expect("search");
        assert!(
            hits.contains(&tricky.id),
            "escaped needle {needle:?} must match"
        );
    }
    let missing_needle = repo
        .find_documents_containing("no such phrase xyz", 10)
        .await
        .expect("miss");
    assert!(!missing_needle.contains(&tricky.id));

    // Backfill report surface: the textless document is a candidate under both
    // bounded and unbounded calls; storing text retires it (dry-run silence
    // is just reading this list without writing).
    let missing_all = repo.missing_document_ids(None).await.expect("missing");
    assert!(missing_all.contains(&no_text.id));
    assert!(!missing_all.contains(&doc.id));
    let missing_one = repo
        .missing_document_ids(Some(10_000))
        .await
        .expect("bounded");
    assert!(missing_one.contains(&no_text.id));
    let count = repo.count().await.expect("count");
    assert!(count >= 2, "at least the two stored texts");

    for id in [doc.id, no_text.id, tricky.id] {
        cleanup_doc(pool, id).await;
    }
    assert!(
        repo.get(doc.id).await.expect("get").is_none(),
        "canonical text is deleted with its document"
    );
}

/// Spans: idempotent and converging resolve, byte-exact canonical slice,
/// parser identity, best-overlap caching, refusal shapes, stale detection.
async fn scn_spans_resolve(pool: &PgPool) {
    const TEXT: &str = "Dabaris opens the ledger. The clerk copies each line twice.";
    let doc = insert_doc(pool, doc_draft("spans-1", 0xA1)).await;
    put_text(pool, doc.id, TEXT, "test", "1.0").await;

    async fn resolve_span(pool: &PgPool, doc: Uuid, s: i64, e: i64) -> SourceSpan {
        let repo = PgSourceSpanRepo::new(pool.clone());
        let mut tx = PgTx::begin(pool).await.expect("begin");
        let span = repo.resolve(&mut tx, doc, s, e).await.expect("resolve");
        tx.commit().await.expect("commit");
        span
    }

    // Idempotent, and concurrent resolves converge on one row.
    let first = resolve_span(pool, doc.id, 8, 20).await;
    let second = resolve_span(pool, doc.id, 8, 20).await;
    assert_eq!(first.id, second.id);
    let (a, b) = tokio::join!(
        resolve_span(pool, doc.id, 8, 20),
        resolve_span(pool, doc.id, 8, 20)
    );
    assert_eq!(a.id, b.id, "races converge on one row");
    assert_eq!(
        PgSourceSpanRepo::new(pool.clone())
            .for_document(doc.id)
            .await
            .expect("list")
            .len(),
        1
    );

    // The stored slice is the canonical text, byte for byte; the caller
    // passes no text. Parser identity rides along.
    let span = resolve_span(pool, doc.id, 0, 7).await;
    assert_eq!(span.quoted_text, "Dabaris");
    assert_eq!(span.parser.as_deref(), Some("test"));
    assert_eq!(span.parser_version.as_deref(), Some("1.0"));

    // Best overlap caches the widest passage; the newest chunker wins ties.
    // (No passages yet: the cache is empty rather than wrong.)
    assert!(span.passage_id.is_none());
    let p1 = insert_passages(
        pool,
        doc.id,
        vec![
            PassageDraft {
                position: 0,
                char_start: 0,
                char_end: 30,
                locator: Map::new(),
                text: TEXT.chars().take(30).collect(),
                token_count: Some(30),
                chunker: "test".to_string(),
                chunker_version: "1.0".to_string(),
                metadata: Map::new(),
                node_id: None,
            },
            PassageDraft {
                position: 1,
                char_start: 0,
                char_end: 30,
                locator: Map::new(),
                text: TEXT.chars().take(30).collect(),
                token_count: Some(30),
                chunker: "test".to_string(),
                chunker_version: "2.0".to_string(),
                metadata: Map::new(),
                node_id: None,
            },
        ],
    )
    .await;
    assert_eq!(p1.len(), 2);
    let overlapped = resolve_span(pool, doc.id, 24, 40).await;
    assert!(
        overlapped.passage_id.is_some(),
        "overlap must cache a passage"
    );

    // Multibyte widths count Unicode scalars, like Python `len(str)`.
    let uni = insert_doc(pool, doc_draft("spans-uni", 0xA2)).await;
    put_text(pool, uni.id, "naïve café ☃ snow", "test", "1.0").await;
    let repo2 = PgSourceSpanRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let uspan = repo2
        .resolve(&mut tx, uni.id, 0, 10)
        .await
        .expect("unicode");
    tx.commit().await.expect("commit");
    let expected: String = "naïve café ☃ snow".chars().take(10).collect();
    assert_eq!(uspan.quoted_text, expected);

    // Refusals: not an address, past the end, and no text at all.
    for (s, e) in [(-1, 5), (5, 5), (9, 3)] {
        let mut tx = PgTx::begin(pool).await.expect("begin");
        let err = repo2
            .resolve(&mut tx, doc.id, s, e)
            .await
            .expect_err("refuse");
        assert_eq!(
            error_kind(&err),
            "validation",
            "({s}, {e}) is not an address"
        );
        tx.rollback().await.expect("rollback");
    }
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let err = repo2
        .resolve(&mut tx, doc.id, 0, TEXT.chars().count() as i64 + 50)
        .await
        .expect_err("past the end");
    assert_eq!(error_kind(&err), "validation");
    tx.rollback().await.expect("rollback");
    let bare = insert_doc(pool, doc_draft("spans-bare", 0xA3)).await;
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let err = repo2
        .resolve(&mut tx, bare.id, 0, 5)
        .await
        .expect_err("no text");
    match err {
        Error::NotFound { kind, .. } => assert_eq!(kind, "document_text"),
        other => panic!("expected document_text NotFound, got {other:?}"),
    }
    tx.rollback().await.expect("rollback");

    // Point lookup hits and misses; document listing is ordered by address.
    let got = repo2.get(span.id).await.expect("get").expect("present");
    assert_eq!(got, span);
    assert!(repo2.get(Uuid::now_v7()).await.expect("miss").is_none());
    let listed = repo2.for_document(doc.id).await.expect("list");
    let starts: Vec<(i64, i64)> = listed.iter().map(|s| (s.char_start, s.char_end)).collect();
    let mut sorted = starts.clone();
    sorted.sort_unstable();
    assert_eq!(starts, sorted, "for_document orders by address");

    // Fresh spans are not stale; a re-parse under a new version stales them.
    assert!(
        !repo2
            .stale(100)
            .await
            .expect("stale")
            .iter()
            .any(|s| s.id == first.id),
        "fresh span is not stale"
    );
    put_text(pool, doc.id, TEXT, "test", "2.0").await;
    assert!(
        repo2
            .stale(100)
            .await
            .expect("stale")
            .iter()
            .any(|s| s.id == first.id),
        "re-parse stales the span"
    );

    for id in [doc.id, uni.id, bare.id] {
        cleanup_doc(pool, id).await;
    }
}

/// Nodes: tree round-trip with resolved parents, depth-limited outline,
/// ltree subtree and ancestor chains, deepest-span lookup, bare root, cascade
/// delete, and the SET NULL rebuild contract.
async fn scn_nodes_tree(pool: &PgPool) {
    let repo = PgDocumentNodeRepo::new(pool.clone());
    let doc = insert_doc(pool, doc_draft("nodes-1", 0xB1)).await;
    let before = repo.count().await.expect("count");
    let stored = insert_tree(pool, doc.id, &book_drafts("A Book")).await;
    assert_eq!(repo.count().await.expect("count"), before + 6);
    let by_title: HashMap<&str, &DocumentNode> = stored
        .iter()
        .map(|n| (n.title.as_deref().unwrap_or(""), n))
        .collect();

    let tree = repo.get_tree(doc.id).await.expect("tree");
    assert_eq!(tree.len(), 6, "five sections plus the synthetic root");
    let root = tree.iter().find(|n| n.path == "r").expect("root");
    assert!(root.parent_id.is_none());
    assert_eq!(root.node_type, "document");
    assert_eq!((root.char_start, root.char_end), (0, 500));
    let ids: std::collections::HashSet<Uuid> = tree.iter().map(|n| n.id).collect();
    for node in &tree {
        if let Some(pid) = node.parent_id {
            assert!(ids.contains(&pid), "parent ids point at real rows");
        }
    }
    assert_eq!(
        by_title["Two, First"].parent_id,
        Some(by_title["Chapter Two"].id)
    );
    assert_eq!(
        by_title["Two, Second, a"].parent_id,
        Some(by_title["Two, Second"].id)
    );

    // Out-of-order drafts refuse before writing anything.
    let bad_doc = insert_doc(pool, doc_draft("nodes-bad", 0xB2)).await;
    let mut drafts = book_drafts("Bad");
    drafts.reverse();
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let err = repo
        .insert_many(&mut tx, bad_doc.id, &drafts)
        .await
        .expect_err("refuse");
    assert_eq!(error_kind(&err), "validation");
    tx.rollback().await.expect("rollback");
    assert!(repo.get_tree(bad_doc.id).await.expect("tree").is_empty());

    let outline = repo.get_outline(doc.id, Some(1)).await.expect("outline");
    assert_eq!(
        outline.iter().map(|n| n.title.clone()).collect::<Vec<_>>(),
        vec![
            Some("A Book".to_string()),
            Some("Chapter One".to_string()),
            Some("Chapter Two".to_string())
        ]
    );
    assert_eq!(repo.get_outline(doc.id, None).await.expect("full").len(), 6);

    let subtree = repo
        .get_subtree(by_title["Chapter Two"].id)
        .await
        .expect("subtree");
    assert_eq!(
        subtree.iter().map(|n| n.title.clone()).collect::<Vec<_>>(),
        vec![
            Some("Chapter Two".to_string()),
            Some("Two, First".to_string()),
            Some("Two, Second".to_string()),
            Some("Two, Second, a".to_string()),
        ]
    );
    assert_eq!(
        repo.get_subtree(by_title["Chapter One"].id)
            .await
            .expect("leaf")
            .len(),
        1
    );

    let chain = repo
        .get_ancestors(by_title["Two, Second, a"].id)
        .await
        .expect("chain");
    assert_eq!(
        chain.iter().map(|n| n.title.clone()).collect::<Vec<_>>(),
        vec![
            Some("A Book".to_string()),
            Some("Chapter Two".to_string()),
            Some("Two, Second".to_string()),
            Some("Two, Second, a".to_string()),
        ]
    );
    assert!(repo
        .get_ancestors_many(&[])
        .await
        .expect("empty")
        .is_empty());
    let deepest: Vec<Uuid> = tree.iter().map(|n| n.id).rev().take(3).collect();
    let batched = repo.get_ancestors_many(&deepest).await.expect("many");
    for id in &deepest {
        let single = repo.get_ancestors(*id).await.expect("single");
        assert_eq!(
            batched
                .get(id)
                .map(|v| v.iter().map(|n| n.id).collect::<Vec<_>>()),
            Some(single.iter().map(|n| n.id).collect::<Vec<_>>()),
            "batched chains agree with one-at-a-time"
        );
    }

    let found = repo
        .find_by_span(doc.id, 410, 420)
        .await
        .expect("span")
        .expect("hit");
    assert_eq!(found.title.as_deref(), Some("Two, Second, a"));
    let straddling = repo
        .find_by_span(doc.id, 50, 250)
        .await
        .expect("span")
        .expect("hit");
    assert_eq!(straddling.node_type, "document");
    assert!(
        repo.find_by_span(bad_doc.id, 0, 10)
            .await
            .expect("miss")
            .is_none(),
        "no tree means no container"
    );

    let got = repo.get(root.id).await.expect("get").expect("present");
    assert_eq!(got, *root);
    assert!(repo.get(Uuid::now_v7()).await.expect("miss").is_none());

    // Passages stamped with their node, then the rebuild that nulls them.
    let passage_repo = PgPassageRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let saved = passage_repo
        .insert_many(
            &mut tx,
            doc.id,
            vec![PassageDraft {
                position: 0,
                char_start: 410,
                char_end: 430,
                locator: Map::new(),
                text: "y".repeat(20),
                token_count: Some(20),
                chunker: "structural".to_string(),
                chunker_version: "3.0".to_string(),
                metadata: Map::new(),
                node_id: Some(by_title["Two, Second, a"].id),
            }],
        )
        .await
        .expect("insert stamped");
    tx.commit().await.expect("commit");
    assert_eq!(saved[0].node_id, Some(by_title["Two, Second, a"].id));

    let direct = passage_repo
        .get_by_node(by_title["Two, Second, a"].id, false)
        .await
        .expect("direct");
    assert_eq!(direct.len(), 1);
    let whole = passage_repo
        .get_by_node(by_title["Chapter Two"].id, true)
        .await
        .expect("descendants");
    assert_eq!(whole.len(), 1, "the one passage sits under Chapter Two");
    assert!(passage_repo
        .get_by_node(Uuid::now_v7(), true)
        .await
        .expect("unknown node")
        .is_empty());

    let mut tx = PgTx::begin(pool).await.expect("begin");
    let removed = repo
        .delete_for_document(&mut tx, doc.id)
        .await
        .expect("delete tree");
    tx.commit().await.expect("commit");
    assert_eq!(removed, 6);
    assert!(repo.get_tree(doc.id).await.expect("tree").is_empty());
    let survivor = passage_repo
        .get(saved[0].id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(survivor.node_id, None, "rebuild nulls, never cascades");
    assert_eq!(survivor.text, "y".repeat(20));

    // Late structure still reaches old passages through the targeted update.
    let stored = insert_tree(pool, doc.id, &book_drafts("A Book")).await;
    let by_title: HashMap<&str, &DocumentNode> = stored
        .iter()
        .map(|n| (n.title.as_deref().unwrap_or(""), n))
        .collect();
    let written = passage_repo
        .set_node_ids(&[(saved[0].id, Some(by_title["Two, Second, a"].id))])
        .await
        .expect("set_node_ids");
    assert_eq!(written, 1);
    let relinked = passage_repo
        .get(saved[0].id)
        .await
        .expect("get")
        .expect("present");
    assert_eq!(relinked.node_id, Some(by_title["Two, Second, a"].id));

    // A document with no structure still gets a bare root.
    let bare_doc = insert_doc(pool, doc_draft("nodes-bare", 0xB3)).await;
    let bare = insert_tree(
        pool,
        bare_doc.id,
        &[DocumentNodeDraft {
            path: "r".to_string(),
            parent_path: None,
            depth: 0,
            position: 0,
            node_type: "document".to_string(),
            title: None,
            char_start: 0,
            char_end: 42,
            metadata: Map::new(),
        }],
    )
    .await;
    assert_eq!(bare.len(), 1);
    assert_eq!(bare[0].char_end, 42);

    // Scalar stored metadata still decodes to `{}`.
    sqlx::query("UPDATE core.document_nodes SET metadata = '\"s\"' WHERE id = $1")
        .bind(bare[0].id)
        .execute(pool)
        .await
        .expect("scalar metadata");
    let decoded = repo.get(bare[0].id).await.expect("get").expect("present");
    assert!(decoded.metadata.is_empty());

    for id in [doc.id, bad_doc.id, bare_doc.id] {
        cleanup_doc(pool, id).await;
    }
}

/// Passages: CRUD with document order, context windows with clamps, covering
/// spans, counts, and the bulk version/locator/node updates with their
/// empty-input and no-match contracts.
async fn scn_passages_crud_and_windows(pool: &PgPool) {
    let repo = PgPassageRepo::new(pool.clone());
    let doc = insert_doc(pool, doc_draft("passages-1", 0xC2)).await;
    put_text(pool, doc.id, DOC_TEXT, "test", "1.0").await;
    let before = repo.count().await.expect("count");

    let texts = ["alpha beta gamma", "delta epsilon", "zeta eta theta iota"];
    let drafts: Vec<PassageDraft> = texts
        .iter()
        .enumerate()
        .map(|(i, t)| {
            let len = t.chars().count() as i64;
            PassageDraft {
                position: i as i64,
                char_start: 0,
                char_end: len,
                locator: Map::new(),
                text: t.to_string(),
                token_count: Some(len),
                chunker: "test".to_string(),
                chunker_version: "1.0".to_string(),
                metadata: Map::new(),
                node_id: None,
            }
        })
        .collect();
    let saved = insert_passages(pool, doc.id, drafts).await;
    assert_eq!(repo.count().await.expect("count"), before + 3);

    let by_doc = repo.get_by_document(doc.id).await.expect("by doc");
    assert_eq!(
        by_doc.iter().map(|p| p.position).collect::<Vec<_>>(),
        vec![0, 1, 2]
    );
    assert_eq!(
        repo.get(saved[0].id)
            .await
            .expect("get")
            .expect("present")
            .text,
        "alpha beta gamma"
    );
    assert!(repo.get(Uuid::now_v7()).await.expect("miss").is_none());

    assert!(repo.get_many(&[]).await.expect("empty").is_empty());
    let many = repo
        .get_many(&[saved[2].id, Uuid::now_v7(), saved[0].id])
        .await
        .expect("get_many");
    assert_eq!(
        many.iter().map(|p| p.id).collect::<Vec<_>>(),
        vec![saved[2].id, saved[0].id]
    );

    // Context windows: the middle passage sees both sides; edges clamp; zero
    // widths answer empty; unknown ids refuse.
    let (b, target, a) = repo.get_context(saved[1].id, 1, 1).await.expect("window");
    assert_eq!(target.id, saved[1].id);
    assert_eq!(
        b.iter().map(|p| p.id).collect::<Vec<_>>(),
        vec![saved[0].id]
    );
    assert_eq!(
        a.iter().map(|p| p.id).collect::<Vec<_>>(),
        vec![saved[2].id]
    );
    let (b, _, a) = repo.get_context(saved[1].id, 0, 0).await.expect("zero");
    assert!(b.is_empty() && a.is_empty());
    let (b, _, _) = repo.get_context(saved[0].id, 5, 0).await.expect("clamp");
    assert!(b.is_empty(), "nothing precedes the first passage");
    let err = repo
        .get_context(Uuid::now_v7(), 1, 1)
        .await
        .expect_err("missing");
    match err {
        Error::NotFound { kind, .. } => assert_eq!(kind, "passage"),
        other => panic!("expected NotFound, got {other:?}"),
    }

    // Covering spans: every passage a document span touches, in offset order.
    let saved_offsets = {
        let repo = PgPassageRepo::new(pool.clone());
        let mut tx = PgTx::begin(pool).await.expect("begin");
        let out = repo
            .insert_many(
                &mut tx,
                doc.id,
                vec![
                    PassageDraft {
                        position: 3,
                        char_start: 0,
                        char_end: 50,
                        locator: Map::new(),
                        text: "x".repeat(50),
                        token_count: Some(50),
                        chunker: "span".to_string(),
                        chunker_version: "1.0".to_string(),
                        metadata: Map::new(),
                        node_id: None,
                    },
                    PassageDraft {
                        position: 4,
                        char_start: 40,
                        char_end: 90,
                        locator: Map::new(),
                        text: "y".repeat(50),
                        token_count: Some(50),
                        chunker: "span".to_string(),
                        chunker_version: "1.0".to_string(),
                        metadata: Map::new(),
                        node_id: None,
                    },
                ],
            )
            .await
            .expect("offset passages");
        tx.commit().await.expect("commit");
        out
    };
    let covering = repo.covering_span(doc.id, 45, 60).await.expect("covering");
    assert_eq!(
        covering.iter().map(|p| p.id).collect::<Vec<_>>(),
        vec![saved_offsets[0].id, saved_offsets[1].id]
    );
    assert!(repo
        .covering_span(doc.id, 5000, 6000)
        .await
        .expect("empty")
        .is_empty());

    // Bulk updates: empty input never touches the database; relabels move the
    // version and token counts; locator/node writes land; a batch matching
    // nothing still reports its size (the `rowcount or len` contract).
    let mut tx = PgTx::begin(pool).await.expect("begin");
    assert_eq!(
        repo.relabel_version(&mut tx, &[], "9.9", &HashMap::new())
            .await
            .expect("empty"),
        0
    );
    let mut counts = HashMap::new();
    counts.insert(saved[0].id, 99_i64);
    assert_eq!(
        repo.relabel_version(&mut tx, &[saved[0].id], "2.0", &counts)
            .await
            .expect("relabel"),
        1
    );
    tx.commit().await.expect("commit");
    // A token-count entry for an id outside `passage_ids` sets only the
    // count (the version `CASE` skips it): exercises the union arm.
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let mut extra_counts = HashMap::new();
    extra_counts.insert(saved[1].id, 7_i64);
    assert_eq!(
        repo.relabel_version(&mut tx, &[saved[0].id], "2.1", &extra_counts)
            .await
            .expect("relabel with outside count"),
        1
    );
    tx.commit().await.expect("commit");
    // Token first: discriminates a `$2` (scope) failure (tokens land,
    // version kept) from a `$3`/`$4` (match) failure (nothing lands).
    let counted = repo.get(saved[1].id).await.expect("get").expect("present");
    assert_eq!(counted.token_count, Some(7));
    let relabeled = repo.get(saved[0].id).await.expect("get").expect("present");
    assert_eq!(relabeled.chunker_version, "2.1");
    assert_eq!(relabeled.token_count, Some(99));
    assert_eq!(repo.set_locators(&[]).await.expect("empty"), 0);
    let mut locator = Map::new();
    locator.insert("page".to_string(), Value::from(7));
    assert_eq!(
        repo.set_locators(&[(saved[1].id, locator.clone())])
            .await
            .expect("locators"),
        1
    );
    let located = repo.get(saved[1].id).await.expect("get").expect("present");
    assert_eq!(located.locator.get("page"), Some(&Value::from(7)));
    // Scalar stored JSON decodes as `{}` rather than crashing (schema drift
    // or a manual write; the columns are NOT NULL-able, so `NULL` cannot
    // occur) — mirrors the Python `row.locator or {}`.
    sqlx::query("UPDATE core.passages SET locator = '[1]', metadata = '\"s\"' WHERE id = $1")
        .bind(saved[1].id)
        .execute(pool)
        .await
        .expect("scalar stored JSON");
    let scalar = repo.get(saved[1].id).await.expect("get").expect("present");
    assert!(scalar.locator.is_empty() && scalar.metadata.is_empty());
    repo.set_locators(&[(saved[1].id, locator.clone())])
        .await
        .expect("restore");
    assert_eq!(
        repo.set_locators(&[(Uuid::now_v7(), locator)])
            .await
            .expect("no match"),
        1,
        "a batch matching nothing still reports its size"
    );

    assert_eq!(repo.set_node_ids(&[]).await.expect("empty"), 0);
    // `node_id` carries a foreign key, so the target must be a real node.
    let anchor = insert_tree(
        pool,
        doc.id,
        &[DocumentNodeDraft {
            path: "r".to_string(),
            parent_path: None,
            depth: 0,
            position: 0,
            node_type: "document".to_string(),
            title: None,
            char_start: 0,
            char_end: 90,
            metadata: Map::new(),
        }],
    )
    .await;
    assert_eq!(
        repo.set_node_ids(&[(saved[2].id, Some(anchor[0].id))])
            .await
            .expect("nodes"),
        1
    );
    assert_eq!(
        repo.get(saved[2].id)
            .await
            .expect("get")
            .expect("present")
            .node_id,
        Some(anchor[0].id)
    );
    // Clearing a link writes a real NULL (not a silent no-op): the `None`
    // array element must survive the `unnest` round trip.
    assert_eq!(
        repo.set_node_ids(&[(saved[2].id, None)])
            .await
            .expect("clear"),
        1
    );
    assert_eq!(
        repo.get(saved[2].id)
            .await
            .expect("get")
            .expect("present")
            .node_id,
        None
    );
    assert_eq!(
        repo.set_node_ids(&[(Uuid::now_v7(), None)])
            .await
            .expect("no match"),
        1
    );

    cleanup_doc(pool, doc.id).await;
}

/// Vector search with bit-exact scores on exact and orthogonal unit vectors,
/// candidate narrowing, `k` limits, `ef_search` parity, and the float32
/// round-trip behind `get_embedding`.
async fn scn_vector_search_exact(pool: &PgPool) {
    const DIM: usize = 1024;
    let repo = PgPassageRepo::new(pool.clone());
    let doc = insert_doc(pool, doc_draft("vector-1", 0xC3)).await;
    let saved = insert_passages(
        pool,
        doc.id,
        vec![
            passage_draft(0, "first passage"),
            passage_draft(1, "second passage"),
            passage_draft(2, "third passage"),
        ],
    )
    .await;
    let ids: Vec<Uuid> = saved.iter().map(|p| p.id).collect();
    let one_hot = |at: usize| {
        let mut v = vec![0.0f64; DIM];
        v[at] = 1.0;
        v
    };
    let e0 = one_hot(0);
    let e1 = one_hot(1);
    let mut e01 = vec![0.0f64; DIM];
    e01[0] = 1.0;
    e01[1] = 1.0;

    // A short embeddings list truncates like Python's non-strict zip: the
    // third passage gets no row.
    let mut tx = PgTx::begin(pool).await.expect("begin");
    repo.store_embeddings(
        &mut tx,
        &ids,
        &[e0.clone(), e1.clone()],
        "test-model",
        "1.0",
        DIM as i64,
    )
    .await
    .expect("store");
    tx.commit().await.expect("commit");
    assert!(repo
        .get_embedding(ids[2], "test-model", "1.0")
        .await
        .expect("get")
        .is_none());
    assert!(
        repo.get_embedding(ids[0], "other-model", "1.0")
            .await
            .expect("miss")
            .is_none(),
        "model and version scope the read"
    );

    // Bit-exact: identical unit vectors score exactly 1.0, orthogonal exactly
    // 0.0. Postgres owns row order, so scores are compared per id.
    let hits = repo
        .vector_search(&e0, "test-model", "1.0", None, 10)
        .await
        .expect("search");
    let scores: HashMap<Uuid, u64> = hits.into_iter().map(|(id, s)| (id, s.to_bits())).collect();
    assert_eq!(
        scores.get(&ids[0]),
        Some(&1.0f64.to_bits()),
        "identical vectors score 1.0"
    );
    assert_eq!(
        scores.get(&ids[1]),
        Some(&0.0f64.to_bits()),
        "orthogonal vectors score 0.0"
    );
    assert!(
        !scores.contains_key(&ids[2]),
        "unembedded passages never match"
    );

    // Narrowing, limits, and the third (diagonal) vector's strict interior
    // score: above orthogonal, below identical.
    let mut tx = PgTx::begin(pool).await.expect("begin");
    repo.store_embeddings(
        &mut tx,
        &[ids[2]],
        &[e01.clone()],
        "test-model",
        "1.0",
        DIM as i64,
    )
    .await
    .expect("store third");
    tx.commit().await.expect("commit");
    let hits = repo
        .vector_search(&e0, "test-model", "1.0", None, 10)
        .await
        .expect("search");
    let scores: HashMap<Uuid, f64> = hits.into_iter().collect();
    assert!(scores[&ids[0]] > scores[&ids[2]] && scores[&ids[2]] > scores[&ids[1]]);
    let narrowed = repo
        .vector_search(&e0, "test-model", "1.0", Some(&[ids[1]]), 10)
        .await
        .expect("narrowed");
    assert_eq!(
        narrowed.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
        vec![ids[1]]
    );
    let limited = repo
        .vector_search(&e0, "test-model", "1.0", None, 2)
        .await
        .expect("limited");
    assert_eq!(limited.len(), 2);
    assert_eq!(
        limited
            .iter()
            .map(|(id, _)| *id)
            .collect::<std::collections::HashSet<_>>(),
        std::collections::HashSet::from([ids[0], ids[2]]),
        "k keeps the two nearest"
    );

    // `ef_search` runs the same statement under SET LOCAL: identical scores.
    let ef_repo = PgPassageRepo::with_ef_search(pool.clone(), 64);
    let ef_hits = ef_repo
        .vector_search(&e0, "test-model", "1.0", None, 10)
        .await
        .expect("ef");
    let ef_scores: HashMap<Uuid, u64> = ef_hits
        .into_iter()
        .map(|(id, s)| (id, s.to_bits()))
        .collect();
    let plain: HashMap<Uuid, u64> = repo
        .vector_search(&e0, "test-model", "1.0", None, 10)
        .await
        .expect("plain")
        .into_iter()
        .map(|(id, s)| (id, s.to_bits()))
        .collect();
    assert_eq!(ef_scores, plain, "ef_search must not change scores");

    // `get_embedding` recovers the stored float32 bits exactly: pgvector
    // prints the shortest round-trip of each stored value, and parsing as
    // `f32` (then widening, like asyncpg) is the exact inverse.
    let mut probe = vec![0.1f64, -2.5, 3.0, 1e-7, 0.0];
    probe.resize(DIM, 0.0);
    let mut tx = PgTx::begin(pool).await.expect("begin");
    repo.store_embeddings(
        &mut tx,
        &[ids[0]],
        &[probe.clone()],
        "probe",
        "9.9",
        DIM as i64,
    )
    .await
    .expect("store probe");
    tx.commit().await.expect("commit");
    let back = repo
        .get_embedding(ids[0], "probe", "9.9")
        .await
        .expect("get")
        .expect("present");
    assert_eq!(back.len(), DIM);
    for (i, (got, want)) in back.iter().zip(probe.iter()).enumerate() {
        let expected = f64::from(*want as f32);
        assert_eq!(
            got.to_bits(),
            expected.to_bits(),
            "element {i} must widen from float32"
        );
    }

    // The Python vector literal format rides through unchanged.
    assert_eq!(
        marginalia_ret::repos::format_vector_py(&[0.1, 0.2]),
        "[0.1,0.2]"
    );

    cleanup_doc(pool, doc.id).await;
}

/// Keyword search per language stemmer, explicit-language routing, the
/// `simple` fallback, unknown-language silence, reindex lang-config moves,
/// and the skip of configs the app does not know.
async fn scn_keyword_multilingual(pool: &PgPool) {
    let repo = PgPassageRepo::new(pool.clone());
    // No FTS rows yet: the unfiltered search answers empty, not an error.
    assert!(
        repo.keyword_search("Haus", None, None, 10)
            .await
            .expect("empty")
            .is_empty(),
        "no configs present means no branches"
    );

    let de_doc = insert_doc(pool, doc_draft("kw-de", 0xC4)).await;
    let de = insert_passages(pool, de_doc.id, vec![passage_draft(0, GERMAN)]).await;
    let en_doc = insert_doc(pool, doc_draft("kw-en", 0xC5)).await;
    let en = insert_passages(pool, en_doc.id, vec![passage_draft(0, ENGLISH)]).await;

    async fn index(pool: &PgPool, id: Uuid, text: &str, cfg: &str) {
        let repo = PgPassageRepo::new(pool.clone());
        let mut tx = PgTx::begin(pool).await.expect("begin");
        repo.index_fts(&mut tx, &[id], &[text.to_string()], cfg)
            .await
            .expect("index");
        tx.commit().await.expect("commit");
    }
    index(pool, de[0].id, GERMAN, "german").await;
    index(pool, en[0].id, ENGLISH, "english").await;

    // Each language is stemmed by its own stemmer across the union.
    let hits = repo
        .keyword_search("Haus", None, Some(&[de[0].id, en[0].id]), 10)
        .await
        .expect("de");
    assert_eq!(
        hits.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
        vec![de[0].id]
    );
    let hits = repo
        .keyword_search("run", None, Some(&[de[0].id, en[0].id]), 10)
        .await
        .expect("en");
    assert_eq!(
        hits.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
        vec![en[0].id]
    );
    for (_, score) in hits {
        assert!(score > 0.0, "matches carry positive rank");
    }

    // German indexed under English is unfindable; re-indexing under German
    // moves `lang_config` across with the vector, restoring the hit.
    let mis_doc = insert_doc(pool, doc_draft("kw-mis", 0xC6)).await;
    let mis = insert_passages(pool, mis_doc.id, vec![passage_draft(0, GERMAN)]).await;
    index(pool, mis[0].id, GERMAN, "english").await;
    assert!(repo
        .keyword_search("Haus", Some("english"), Some(&[mis[0].id]), 10)
        .await
        .expect("miss")
        .is_empty());
    index(pool, mis[0].id, GERMAN, "german").await;
    let cfg: String =
        sqlx::query_scalar("SELECT lang_config::text FROM core.passage_fts WHERE passage_id = $1")
            .bind(mis[0].id)
            .fetch_one(pool)
            .await
            .expect("lang_config");
    assert_eq!(
        cfg, "german",
        "reindex moves the config, not just the vector"
    );
    assert_eq!(
        repo.keyword_search("Haus", None, Some(&[mis[0].id]), 10)
            .await
            .expect("hit")
            .iter()
            .map(|(id, _)| *id)
            .collect::<Vec<_>>(),
        vec![mis[0].id]
    );

    // Explicit language restricts to one branch; unknown languages refuse by
    // answering empty rather than interpolating.
    assert!(!repo
        .keyword_search("Haus", Some("german"), Some(&[mis[0].id]), 10)
        .await
        .expect("de")
        .is_empty());
    assert!(repo
        .keyword_search("Haus", Some("english"), Some(&[mis[0].id]), 10)
        .await
        .expect("en")
        .is_empty());
    assert!(repo
        .keyword_search("Haus", Some("xx"), Some(&[mis[0].id]), 10)
        .await
        .expect("xx")
        .is_empty());

    // `simple` under-matches by design: no stemming, exact tokens only.
    let simple_doc = insert_doc(pool, doc_draft("kw-simple", 0xC7)).await;
    let simple = insert_passages(pool, simple_doc.id, vec![passage_draft(0, ENGLISH)]).await;
    index(pool, simple[0].id, ENGLISH, "simple").await;
    assert!(repo
        .keyword_search("run", Some("simple"), Some(&[simple[0].id]), 10)
        .await
        .expect("stem")
        .is_empty());
    assert!(!repo
        .keyword_search("running", Some("simple"), Some(&[simple[0].id]), 10)
        .await
        .expect("exact")
        .is_empty());

    // Unknown FTS languages are skipped, never interpolated: a row the app
    // does not know (`catalan` is a real regconfig outside the app table)
    // stays invisible to the unfiltered search.
    let cat_doc = insert_doc(pool, doc_draft("kw-cat", 0xC8)).await;
    let cat = insert_passages(pool, cat_doc.id, vec![passage_draft(0, "els gats corren")]).await;
    sqlx::query(
        "INSERT INTO core.passage_fts (passage_id, lang_config, ts) VALUES \
         ($1, CAST('catalan' AS regconfig), to_tsvector(CAST('catalan' AS regconfig), 'els gats corren'))",
    )
    .bind(cat[0].id)
    .execute(pool)
    .await
    .expect("catalan fts row");
    assert!(
        repo.keyword_search("gats", None, None, 10)
            .await
            .expect("skip")
            .iter()
            .all(|(id, _)| *id != cat[0].id),
        "unknown configs contribute no branch"
    );

    // An unvalidated regconfig refuses at the index call with the Python
    // message shape.
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let err = repo
        .index_fts(&mut tx, &[cat[0].id], &["x".to_string()], "xx")
        .await
        .expect_err("unknown config");
    assert_eq!(error_kind(&err), "validation");
    assert!(
        err.to_string().contains("Unknown text-search config: 'xx'"),
        "{err:?}"
    );
    tx.rollback().await.expect("rollback");

    // The union shape keeps one constant tsquery per branch, which is what
    // lets the GIN index apply (sequential scans forced off so the shape,
    // not the table size, is what is asserted).
    let mut conn = pool.acquire().await.expect("acquire");
    sqlx::query("BEGIN")
        .execute(&mut *conn)
        .await
        .expect("begin");
    sqlx::query("SET LOCAL enable_seqscan = off")
        .execute(&mut *conn)
        .await
        .expect("setting");
    let plan: Vec<String> = sqlx::query_scalar(
        "EXPLAIN SELECT pf.passage_id FROM core.passage_fts pf, \
         plainto_tsquery('german', 'Haus') AS q(tsq) WHERE pf.lang_config = 'german'::regconfig \
         AND pf.ts @@ q.tsq",
    )
    .fetch_all(&mut *conn)
    .await
    .expect("explain");
    sqlx::query("ROLLBACK")
        .execute(&mut *conn)
        .await
        .expect("rollback");
    assert!(
        plan.iter().any(|line| line.contains("passage_fts_ts_idx")),
        "union branches must use the GIN index: {plan:?}"
    );

    for id in [de_doc.id, en_doc.id, mis_doc.id, simple_doc.id, cat_doc.id] {
        cleanup_doc(pool, id).await;
    }
}

/// Candidate filtering: every pure branch end to end, both builtin extension
/// shapes (AND intersection and OR union), date bounds as RFC3339 strings,
/// entity-name resolution (present, empty, absent), and every refusal shape.
async fn scn_filter_candidates(pool: &PgPool) {
    let repo = PgPassageRepo::new(pool.clone());
    let start = Utc.with_ymd_and_hms(1820, 6, 1, 0, 0, 0).unwrap();
    let end = Utc.with_ymd_and_hms(1830, 6, 1, 0, 0, 0).unwrap();
    let mut book = doc_draft("cand-book", 0xD3);
    book.document_type = "book".to_string();
    book.language = Some("en".to_string());
    book.created_date_start = Some(start);
    book.created_date_end = Some(end);
    let mut book_meta = Map::new();
    book_meta.insert("author".to_string(), Value::from("The Clerk"));
    // The recipient carries the alias form, so the recipient filter proves
    // alias resolution rather than re-proving the canonical name.
    book_meta.insert("recipient".to_string(), Value::from("Clerk"));
    book.metadata = book_meta;
    let book = insert_doc(pool, book).await;
    let mut article = doc_draft("cand-article", 0xD4);
    article.document_type = "article".to_string();
    article.language = Some("de".to_string());
    let article = insert_doc(pool, article).await;

    let mut passage_meta = Map::new();
    passage_meta.insert("k".to_string(), Value::from("v"));
    let book_passages = insert_passages(
        pool,
        book.id,
        vec![PassageDraft {
            position: 0,
            char_start: 0,
            char_end: 5,
            locator: Map::new(),
            text: "clerk".to_string(),
            token_count: Some(1),
            chunker: "test".to_string(),
            chunker_version: "1.0".to_string(),
            metadata: passage_meta,
            node_id: None,
        }],
    )
    .await;
    let article_passages = insert_passages(pool, article.id, vec![passage_draft(0, "other")]).await;

    // Entities: a named one with an alias, and a nameless one whose empty
    // resolution must match nothing rather than everything.
    let clerk = Uuid::now_v7();
    let nameless = Uuid::now_v7();
    sqlx::query("INSERT INTO core.entities (id, entity_type, canonical_name) VALUES ($1, 'person', 'The Clerk'), ($2, 'person', '')")
        .bind(clerk)
        .bind(nameless)
        .execute(pool)
        .await
        .expect("entities");
    sqlx::query("INSERT INTO core.entity_aliases (entity_id, alias) VALUES ($1, 'Clerk')")
        .bind(clerk)
        .execute(pool)
        .await
        .expect("alias");
    sqlx::query(
        "INSERT INTO core.mentions (id, passage_id, entity_id, surface_form, confidence, source) \
         VALUES ($1, $2, $3, 'the clerk', 0.9, 'test')",
    )
    .bind(Uuid::now_v7())
    .bind(book_passages[0].id)
    .bind(clerk)
    .execute(pool)
    .await
    .expect("mention");

    let filters = |pairs: &[(&str, Value)]| {
        let mut m = Map::new();
        for (k, v) in pairs {
            m.insert(k.to_string(), v.clone());
        }
        m
    };

    // Document-type, date, language, mentions, and metadata branches compose.
    let hits = repo
        .filter_candidate_ids::<ExtensionClause>(
            &filters(&[
                ("document_types", Value::Array(vec![Value::from("book")])),
                ("date_range_start", Value::from("1800-01-01T00:00:00Z")),
                ("date_range_end", Value::from("1850-01-01T00:00:00Z")),
                ("language", Value::from("en")),
                (
                    "mentions_entity_ids",
                    Value::Array(vec![Value::from(clerk.to_string())]),
                ),
                ("metadata", serde_json::json!({"k": "v"})),
            ]),
            None,
        )
        .await
        .expect("composed");
    assert_eq!(hits, vec![book_passages[0].id]);

    // Author and recipient resolve through entity names against document
    // metadata; an unknown entity id resolves to no names and matches nothing.
    let hits = repo
        .filter_candidate_ids::<ExtensionClause>(
            &filters(&[("author_entity_id", Value::from(clerk.to_string()))]),
            None,
        )
        .await
        .expect("author");
    assert!(hits.contains(&book_passages[0].id));
    assert!(!hits.contains(&article_passages[0].id));
    let hits = repo
        .filter_candidate_ids::<ExtensionClause>(
            &filters(&[("recipient_entity_id", Value::from(clerk.to_string()))]),
            None,
        )
        .await
        .expect("recipient alias");
    assert!(
        hits.contains(&book_passages[0].id),
        "alias 'Clerk' matches 'The Clerk'"
    );
    for key in ["author_entity_id", "recipient_entity_id"] {
        let hits = repo
            .filter_candidate_ids::<ExtensionClause>(
                &filters(&[(key, Value::from(nameless.to_string()))]),
                None,
            )
            .await
            .expect("nameless");
        assert!(
            hits.is_empty(),
            "{key} with no names matches nothing, never everything"
        );
        let hits = repo
            .filter_candidate_ids::<ExtensionClause>(
                &filters(&[(key, Value::from(Uuid::now_v7().to_string()))]),
                None,
            )
            .await
            .expect("unknown entity");
        assert!(hits.is_empty());
    }

    // Refusals: unknown keys, unregistered extensions, malformed shapes.
    let err = repo
        .filter_candidate_ids::<ExtensionClause>(&filters(&[("bogus", Value::from(1))]), None)
        .await
        .expect_err("unknown key");
    assert_eq!(error_kind(&err), "unsupported");
    let err = repo
        .filter_candidate_ids::<ExtensionClause>(
            &filters(&[("extensions", serde_json::json!({"nope": 1}))]),
            None,
        )
        .await
        .expect_err("unregistered");
    assert_eq!(error_kind(&err), "unknown_extension");
    // The generic port method cannot execute prebuilt clauses: it fails loud
    // naming the clause type instead of running an unfiltered query.
    let mut registry: HashMap<String, ExtensionClause> = HashMap::new();
    registry.insert(
        "has_extraction".to_string(),
        ExtensionClause {
            sql: "SELECT 1".to_string(),
            params: vec![],
        },
    );
    let err = repo
        .filter_candidate_ids(
            &filters(&[("extensions", serde_json::json!({"has_extraction": 1}))]),
            Some(&registry),
        )
        .await
        .expect_err("generic extensions refuse");
    assert_eq!(error_kind(&err), "plugin");
    for bad in [
        filters(&[("document_types", Value::from("nope"))]),
        filters(&[("mentions_entity_ids", Value::from("nope"))]),
        filters(&[(
            "mentions_entity_ids",
            Value::Array(vec![Value::from("not-a-uuid")]),
        )]),
        filters(&[("extensions", Value::from("nope"))]),
        filters(&[("date_range_start", Value::from("yesterday"))]),
        filters(&[("author_entity_id", Value::from("nope"))]),
    ] {
        let err = repo
            .filter_candidate_ids::<ExtensionClause>(&bad, None)
            .await
            .expect_err("malformed shape must refuse");
        assert_eq!(error_kind(&err), "validation", "for {bad:?}");
    }

    // Both builtin extension shapes through the prebuilt-clause registry: AND
    // intersects per-clause INs (with outer params offsetting placeholders),
    // OR unions them into one.
    let mention_clause = |entity: Uuid| ExtensionClause {
        sql: "SELECT m.passage_id FROM core.mentions m WHERE m.entity_id = $1".to_string(),
        params: vec![Param::Uuid(entity)],
    };
    let mut clauses: HashMap<String, ExtensionClause> = HashMap::new();
    clauses.insert("mentions_clerk".to_string(), mention_clause(clerk));
    clauses.insert("mentions_nameless".to_string(), mention_clause(nameless));
    let and_hits = repo
        .filter_candidate_ids_with_clauses(
            &filters(&[
                ("language", Value::from("en")),
                (
                    "extensions",
                    serde_json::json!({"mentions_clerk": null, "mentions_nameless": null}),
                ),
            ]),
            &clauses,
        )
        .await
        .expect("and");
    assert!(
        and_hits.is_empty(),
        "AND intersects: no passage mentions both entities"
    );
    let and_hits = repo
        .filter_candidate_ids_with_clauses(
            &filters(&[
                ("language", Value::from("en")),
                ("extensions", serde_json::json!({"mentions_clerk": null})),
            ]),
            &clauses,
        )
        .await
        .expect("and single");
    assert_eq!(and_hits, vec![book_passages[0].id]);
    let or_hits = repo
        .filter_candidate_ids_with_clauses(
            &filters(&[
                (
                    "extensions",
                    serde_json::json!({"mentions_clerk": null, "mentions_nameless": null}),
                ),
                ("extension_logic", Value::from("or")),
            ]),
            &clauses,
        )
        .await
        .expect("or");
    assert_eq!(or_hits, vec![book_passages[0].id], "OR unions both clauses");

    cleanup_entities(pool, &[clerk, nameless]).await;
    for id in [book.id, article.id] {
        cleanup_doc(pool, id).await;
    }
}

/// The lemma survey against the seeded reference rows: exact totals, agreeing
/// aggregates, citable occurrences, the versification hop, the map-loaded
/// guard (with rollback), homographs, narrowing, zero and collision shapes,
/// and the over-limit refusal.
async fn scn_lemma_survey(pool: &PgPool, scratch: &Scratch) {
    let lookup = PgLemmaLookup::new(pool.clone());

    assert!(lookup.verse_map_is_loaded().await.expect("loaded"));
    let map_rows: i64 = sqlx::query_scalar("SELECT count(*) FROM core.verse_map")
        .fetch_one(pool)
        .await
        .expect("map count");
    assert_eq!(
        map_rows, 1978,
        "the map rollback must leave the corpus untouched"
    );

    // Emptied inside a transaction that rolls back: unloaded reads false,
    // every English reference withholds as unmapped, and the corpus is intact
    // afterwards.
    let mut conn = pool.acquire().await.expect("acquire");
    sqlx::query("BEGIN")
        .execute(&mut *conn)
        .await
        .expect("begin");
    sqlx::query("DELETE FROM core.verse_map")
        .execute(&mut *conn)
        .await
        .expect("empty map");
    let empty: Option<i32> = sqlx::query_scalar(
        "SELECT 1 FROM core.verse_map WHERE from_scheme = $1 AND to_scheme = $2 LIMIT 1",
    )
    .bind("hebrew")
    .bind("english")
    .fetch_optional(&mut *conn)
    .await
    .expect("probe");
    assert!(empty.is_none(), "emptied map probes empty");
    sqlx::query("ROLLBACK")
        .execute(&mut *conn)
        .await
        .expect("rollback");
    // A lookup while the map is empty withholds every English reference and
    // says so first; the map is re-seeded afterwards from the dev database
    // (read-only COPY source, never written).
    sqlx::query("DELETE FROM core.verse_map")
        .execute(pool)
        .await
        .expect("empty map");
    assert!(!lookup.verse_map_is_loaded().await.expect("unloaded"));
    let unmapped = lookup
        .find(&LemmaQuery::new("4941"))
        .await
        .expect("unmapped find");
    assert_eq!(unmapped.total, 422, "totals never needed the map");
    assert!(
        unmapped
            .notes
            .iter()
            .any(|n| n.contains("core.verse_map is empty")),
        "map-empty note leads: {:?}",
        unmapped.notes
    );
    assert!(
        unmapped
            .occurrences
            .iter()
            .all(|o| o.english.mapping == Mapping::Unmapped && o.english.r#ref.is_none()),
        "unmapped withholds instead of guessing"
    );
    copy_table(
        &dev_url(),
        &scratch.url,
        "COPY core.verse_map TO STDOUT",
        "COPY core.verse_map FROM STDIN",
    );
    sqlx::query("SELECT setval('core.verse_map_id_seq', (SELECT max(id) FROM core.verse_map))")
        .execute(pool)
        .await
        .expect("reseed sequence");
    assert!(lookup.verse_map_is_loaded().await.expect("reseeded"));

    // Exact survey counts, and aggregates that agree with the list they
    // summarise (they run as their own queries, so they can drift).
    let mishpat = lookup
        .find(&LemmaQuery::new("4941"))
        .await
        .expect("mishpat");
    assert_eq!((mishpat.total, mishpat.books), (422, 31));
    assert_eq!(mishpat.occurrences.len(), 422);
    let tsedaqah = lookup
        .find(&LemmaQuery::new("6666"))
        .await
        .expect("tsedaqah");
    assert_eq!((tsedaqah.total, tsedaqah.books), (157, 22));
    for (key, rows) in [
        (
            "by_surface",
            mishpat
                .counts
                .by_surface
                .iter()
                .map(|r| r.count)
                .sum::<i64>(),
        ),
        (
            "by_book",
            mishpat.counts.by_book.iter().map(|r| r.count).sum::<i64>(),
        ),
        (
            "by_morph",
            mishpat.counts.by_morph.iter().map(|r| r.count).sum::<i64>(),
        ),
        (
            "by_prefixes",
            mishpat
                .counts
                .by_prefixes
                .iter()
                .map(|r| r.count)
                .sum::<i64>(),
        ),
    ] {
        assert_eq!(rows, mishpat.total, "{key} sums to the total");
    }
    assert_eq!(mishpat.counts.by_book.len() as i64, mishpat.books);
    let by_prefix: HashMap<&str, i64> = mishpat
        .counts
        .by_prefixes
        .iter()
        .map(|r| (r.prefixes.as_str(), r.count))
        .collect();
    assert_eq!(
        by_prefix.get("k"),
        Some(&37),
        "k/4941 is an idiom, not noise"
    );
    assert_eq!(by_prefix.get("b"), Some(&33));

    // Occurrences are citable verse identities: dotted refs that echo their
    // parts, morphology on every row, canonical book order.
    for o in &tsedaqah.occurrences {
        assert_eq!(o.r#ref.matches('.').count(), 2);
        assert_eq!(o.r#ref, format!("{}.{}.{}", o.book, o.chapter, o.verse));
    }
    assert!(mishpat.occurrences.iter().all(|o| !o.morph.is_empty()));
    assert!(mishpat
        .occurrences
        .iter()
        .any(|o| o.prefixes.as_deref().is_some_and(|p| !p.is_empty())));
    let books: Vec<&str> = tsedaqah
        .counts
        .by_book
        .iter()
        .map(|r| r.book.as_str())
        .collect();
    assert_eq!(books.first(), Some(&"Gen"));
    let pos = |b: &str| books.iter().position(|x| *x == b).expect("book present");
    assert!(pos("Deut") < pos("Isa") && pos("Isa") < pos("Mal"));

    // The versification hop: divergent verses carry a different English ref;
    // unmapped ones report the same ref on both sides with the exact shape.
    let mapped: Vec<_> = mishpat
        .occurrences
        .iter()
        .filter(|o| o.english.mapping != Mapping::Same)
        .collect();
    assert!(!mapped.is_empty(), "some mishpat verses diverge");
    for o in mapped {
        assert_ne!(
            o.english.r#ref.as_deref(),
            Some(o.r#ref.as_str()),
            "divergent verses move"
        );
    }
    let gen = lookup
        .find(&LemmaQuery {
            book: Some("Gen".to_string()),
            ..LemmaQuery::new("4941")
        })
        .await
        .expect("gen");
    assert!(!gen.occurrences.is_empty());
    for o in &gen.occurrences {
        assert_eq!(o.english.r#ref.as_deref(), Some(o.r#ref.as_str()));
        assert_eq!(o.english.mapping, Mapping::Same);
        assert_eq!(o.english.part, None);
        assert_eq!(o.english.hebrew_part, None);
    }

    // Notes: partial splits and qere forms are announced, not silent.
    let partial_carrier = lookup.find(&LemmaQuery::new("2151")).await.expect("2151");
    assert!(
        partial_carrier
            .notes
            .iter()
            .any(|n| n.contains("splits or joins")),
        "partial mappings are announced: {:?}",
        partial_carrier.notes
    );
    assert!(partial_carrier
        .occurrences
        .iter()
        .any(|o| o.english.mapping == Mapping::Partial));
    assert!(
        mishpat.notes.iter().any(|n| n.contains("qere")),
        "qere forms are announced: {:?}",
        mishpat.notes
    );
    assert!(mishpat.occurrences.iter().any(|o| o.from_qere));

    // Homographs: the split sums to the whole; the empty string asks only
    // for rows without a letter.
    let row: (String, i64) = sqlx::query_as(
        "SELECT strong, count(DISTINCT homograph) AS n FROM core.words \
         WHERE homograph IS NOT NULL GROUP BY 1 HAVING count(DISTINCT homograph) > 1 \
         ORDER BY n DESC, strong LIMIT 1",
    )
    .fetch_one(pool)
    .await
    .expect("split strong");
    assert!(row.1 > 1, "the corpus holds a number OSHB split");
    let every = lookup
        .find(&LemmaQuery {
            include_occurrences: false,
            ..LemmaQuery::new(&row.0)
        })
        .await
        .expect("every");
    let mut accounted = 0;
    for part in &every.counts.by_homograph {
        let split = lookup
            .find(&LemmaQuery {
                homograph: Some(part.homograph.clone()),
                include_occurrences: false,
                ..LemmaQuery::new(&row.0)
            })
            .await
            .expect("split");
        assert!(split.total < every.total);
        accounted += split.total;
    }
    assert_eq!(accounted, every.total);
    let bare_rows = lookup
        .find(&LemmaQuery {
            homograph: Some(String::new()),
            ..LemmaQuery::new("4941")
        })
        .await
        .expect("bare");
    assert_eq!(bare_rows.total, 422);
    assert!(bare_rows.occurrences.iter().all(|o| o.homograph.is_none()));

    // Narrowing: book, chapter range, and the language index boundary.
    let one = lookup
        .find(&LemmaQuery {
            book: Some("Ps".to_string()),
            ..LemmaQuery::new("4941")
        })
        .await
        .expect("ps");
    assert!(one.total > 0 && one.total < mishpat.total);
    assert!(one.occurrences.iter().all(|o| o.book == "Ps"));
    let ranged = lookup
        .find(&LemmaQuery {
            book: Some("Ps".to_string()),
            chapter_start: Some(1),
            chapter_end: Some(50),
            ..LemmaQuery::new("4941")
        })
        .await
        .expect("ranged");
    assert!(ranged.total > 0);
    assert!(ranged
        .occurrences
        .iter()
        .all(|o| (1..=50).contains(&o.chapter)));
    assert_eq!(ranged.query.chapters, Some([Some(1), Some(50)]));
    // A chapter bound alone still numbers placeholders from three: covered
    // here through the Int bind arm on a real query.
    let start_only = lookup
        .find(&LemmaQuery {
            chapter_start: Some(100),
            ..LemmaQuery::new("4941")
        })
        .await
        .expect("start only");
    assert!(start_only.occurrences.iter().all(|o| o.chapter >= 100));

    // Zero and collision shapes: the hint names the number; counts ride as
    // `{}` on the wire; Greek never collides with Hebrew.
    let zero = lookup
        .find(&LemmaQuery::new("99999999"))
        .await
        .expect("zero");
    assert_eq!((zero.total, zero.occurrences.len()), (0, 0));
    assert!(!zero.notes.is_empty() && zero.notes[0].contains("99999999"));
    let wire = serde_json::to_value(&zero).expect("wire");
    assert_eq!(wire.get("counts"), Some(&Value::Object(Map::new())));
    assert_eq!(
        lookup
            .find(&LemmaQuery {
                language: "grc".to_string(),
                ..LemmaQuery::new("4941")
            })
            .await
            .expect("grc")
            .total,
        0
    );
    assert_eq!(
        lookup
            .find(&LemmaQuery {
                language: "he".to_string(),
                ..LemmaQuery::new("4941")
            })
            .await
            .expect("he")
            .total,
        422
    );

    // Over the cap: refused with counts intact, never truncated silently.
    let huge = lookup
        .find(&LemmaQuery {
            include_occurrences: false,
            ..LemmaQuery::new("853")
        })
        .await
        .expect("huge");
    assert!(huge.total > 2000, "853 must exceed the cap");
    let huge_listed = lookup
        .find(&LemmaQuery::new("853"))
        .await
        .expect("huge listed");
    assert!(
        huge_listed.occurrences.is_empty(),
        "past the cap nothing enumerates"
    );
    assert!(
        huge_listed
            .notes
            .iter()
            .any(|n| n.contains("over the 2000 limit")),
        "refusal says so: {:?}",
        huge_listed.notes
    );
    assert!(
        !huge_listed.counts.is_empty(),
        "aggregates describe the whole set"
    );

    // Not enumerating still aggregates and still guards the map.
    let lean = lookup
        .find(&LemmaQuery {
            include_occurrences: false,
            ..LemmaQuery::new("4941")
        })
        .await
        .expect("lean");
    assert_eq!(lean.total, 422);
    assert!(lean.occurrences.is_empty());
    assert!(!lean.counts.is_empty());

    // Known books arrive in canonical order.
    let books = lookup.known_books("he").await.expect("books");
    assert!(!books.is_empty());
    assert_eq!(books.first().map(String::as_str), Some("Gen"));
    assert!(books.contains(&"Mal".to_string()));
    assert!(lookup.known_books("xx").await.expect("unknown").is_empty());
}

/// Versification reference rows the lemma survey rests on: the map is fully
/// loaded, partials carry halves, full mappings never do, the hand-surveyed
/// verses resolve, 27 books map, and Joel.4.1 lands on Joel.3.1.
async fn scn_versification_reference(pool: &PgPool) {
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM core.verse_map")
        .fetch_one(pool)
        .await
        .expect("count");
    assert_eq!(count, 1978);

    let partials: Vec<(String, Option<String>, String, Option<String>)> = sqlx::query_as(
        "SELECT from_ref, from_part, to_ref, to_part FROM core.verse_map \
         WHERE mapping_type = 'partial' ORDER BY from_ref, from_part",
    )
    .fetch_all(pool)
    .await
    .expect("partials");
    assert_eq!(partials.len(), 7);
    assert!(partials
        .iter()
        .all(|(_, fp, _, tp)| fp.is_some() || tp.is_some()));
    assert!(
        partials.contains(&(
            "Isa.63.19".to_string(),
            Some("b".to_string()),
            "Isa.64.1".to_string(),
            None
        )),
        "a verse beginning midway through another"
    );
    let full_with_half: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM core.verse_map WHERE mapping_type = 'full' \
         AND (from_part IS NOT NULL OR to_part IS NOT NULL)",
    )
    .fetch_one(pool)
    .await
    .expect("full halves");
    assert_eq!(full_with_half, 0);

    for (hebrew, english) in [
        ("Hos.2.21", "Hos.2.19"),
        ("Isa.9.6", "Isa.9.7"),
        ("Jer.9.23", "Jer.9.24"),
        ("Ps.36.6", "Ps.36.5"),
        ("Ps.89.15", "Ps.89.14"),
    ] {
        let got: Option<String> = sqlx::query_scalar(
            "SELECT to_ref FROM core.verse_map WHERE from_scheme = 'hebrew' \
             AND to_scheme = 'english' AND from_ref = $1",
        )
        .bind(hebrew)
        .fetch_optional(pool)
        .await
        .expect("mapping");
        assert_eq!(got.as_deref(), Some(english), "hand-surveyed {hebrew}");
    }

    let mapped_books: i64 = sqlx::query_scalar(
        "SELECT count(DISTINCT split_part(from_ref, '.', 1)) FROM core.verse_map",
    )
    .fetch_one(pool)
    .await
    .expect("mapped books");
    assert_eq!(mapped_books, 27);
    let joel: Option<String> =
        sqlx::query_scalar("SELECT to_ref FROM core.verse_map WHERE from_ref = 'Joel.4.1'")
            .fetch_optional(pool)
            .await
            .expect("joel");
    assert_eq!(joel.as_deref(), Some("Joel.3.1"));
}

/// Reindex shapes at the repo level: old passages go away, new ones carry
/// true offsets into the canonical text, both search indexes cover the new
/// rows, and identical chunker output relabels in place.
async fn scn_reindex_like_flow(pool: &PgPool) {
    const TEXT: &str = "The archive holds letters from the campaign. Each letter carries a date. \
        Some dates are approximate, written from memory. Others are exact, recorded by the \
        clerk who filed them. A final note closes the folder.";
    let text_repo = PgDocumentTextRepo::new(pool.clone());
    let passage_repo = PgPassageRepo::new(pool.clone());
    let doc = insert_doc(pool, doc_draft("reindex-1", 0xD5)).await;
    put_text(pool, doc.id, TEXT, "test", "1.0").await;

    // Old-style passages: collapsed text with stale spans.
    let halves = [&TEXT[..140], &TEXT[140..]];
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let old = passage_repo
        .insert_many(
            &mut tx,
            doc.id,
            halves
                .iter()
                .enumerate()
                .map(|(i, half)| PassageDraft {
                    position: i as i64,
                    char_start: 0,
                    char_end: half
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ")
                        .chars()
                        .count() as i64,
                    locator: Map::new(),
                    text: half.split_whitespace().collect::<Vec<_>>().join(" "),
                    token_count: Some(10),
                    chunker: "prose_window".to_string(),
                    chunker_version: "1.0".to_string(),
                    metadata: Map::new(),
                    node_id: None,
                })
                .collect(),
        )
        .await
        .expect("old passages");
    const DIM: usize = 1024;
    let emb: Vec<f64> = vec![0.5; DIM];
    passage_repo
        .store_embeddings(
            &mut tx,
            &[old[0].id],
            std::slice::from_ref(&emb),
            "test-model",
            "1.0",
            DIM as i64,
        )
        .await
        .expect("old embeddings");
    passage_repo
        .index_fts(&mut tx, &[old[0].id], &[old[0].text.clone()], "english")
        .await
        .expect("old fts");
    tx.commit().await.expect("commit");
    let old_ids: Vec<Uuid> = old.iter().map(|p| p.id).collect();

    // Re-chunk: delete the stale rows, write true-offset ones, re-index both.
    sqlx::query("DELETE FROM core.passages WHERE id = ANY($1)")
        .bind(&old_ids)
        .execute(pool)
        .await
        .expect("delete old");
    let new_texts = [&TEXT[..100], &TEXT[100..200], &TEXT[200..]];
    let new_drafts: Vec<PassageDraft> = new_texts
        .iter()
        .enumerate()
        .map(|(i, t)| {
            let start = TEXT.find(t).expect("offset");
            PassageDraft {
                position: i as i64,
                char_start: start as i64,
                char_end: (start + t.len()) as i64,
                locator: Map::new(),
                text: t.to_string(),
                token_count: Some(10),
                chunker: "prose_window".to_string(),
                chunker_version: "2.0".to_string(),
                metadata: Map::new(),
                node_id: None,
            }
        })
        .collect();
    let new_saved = insert_passages(pool, doc.id, new_drafts.clone()).await;
    let new_ids: Vec<Uuid> = new_saved.iter().map(|p| p.id).collect();
    assert!(
        new_ids.iter().all(|id| !old_ids.contains(id)),
        "old passages are gone"
    );

    let canonical = text_repo
        .get_text(doc.id)
        .await
        .expect("text")
        .expect("present");
    for p in passage_repo.get_by_document(doc.id).await.expect("by doc") {
        let (s, e) = (p.char_start.expect("start"), p.char_end.expect("end"));
        let slice: String = canonical
            .chars()
            .skip(s as usize)
            .take((e - s) as usize)
            .collect();
        assert_eq!(slice, p.text, "new passages carry true offsets");
    }

    let mut tx = PgTx::begin(pool).await.expect("begin");
    let embs: Vec<Vec<f64>> = new_saved.iter().map(|_| vec![0.25; DIM]).collect();
    passage_repo
        .store_embeddings(&mut tx, &new_ids, &embs, "test-model", "1.0", DIM as i64)
        .await
        .expect("new embeddings");
    let fts_texts: Vec<String> = new_saved.iter().map(|p| p.text.clone()).collect();
    passage_repo
        .index_fts(&mut tx, &new_ids, &fts_texts, "english")
        .await
        .expect("new fts");
    tx.commit().await.expect("commit");

    // Both indexes cover exactly the new rows: the document stays searchable.
    let embedded: i64 = sqlx::query_scalar(
        "SELECT count(DISTINCT passage_id) FROM core.passage_embeddings WHERE passage_id = ANY($1)",
    )
    .bind(&new_ids)
    .fetch_one(pool)
    .await
    .expect("embedded");
    assert_eq!(
        embedded,
        new_ids.len() as i64,
        "new passages have embeddings"
    );
    let indexed: i64 =
        sqlx::query_scalar("SELECT count(*) FROM core.passage_fts WHERE passage_id = ANY($1)")
            .bind(&new_ids)
            .fetch_one(pool)
            .await
            .expect("indexed");
    assert_eq!(
        indexed,
        new_ids.len() as i64,
        "new passages are in the FTS index"
    );
    let hits = passage_repo
        .vector_search(&vec![0.25; DIM], "test-model", "1.0", Some(&new_ids), 10)
        .await
        .expect("vector after reindex");
    assert_eq!(hits.len(), new_ids.len());
    let kw = passage_repo
        .keyword_search("campaign", Some("english"), Some(&new_ids), 10)
        .await
        .expect("keyword after reindex");
    assert!(!kw.is_empty());

    // Identical chunker output relabels in place instead of rewriting.
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let relabeled = passage_repo
        .relabel_version(&mut tx, &new_ids, "2.1", &HashMap::new())
        .await
        .expect("relabel");
    tx.commit().await.expect("commit");
    assert_eq!(relabeled, new_ids.len() as u64);

    // Structure rebuild: headings read back out of the canonical text give a
    // rooted tree whose spans address the text, and passages gain owners.
    let md = "# The Peninsula\n\nThe clerk recorded the transaction in the ledger.\n";
    let struct_doc = insert_doc(pool, doc_draft("reindex-struct", 0xD6)).await;
    put_text(pool, struct_doc.id, md, "test", "1.0").await;
    let struct_tree = insert_tree(
        pool,
        struct_doc.id,
        &[
            DocumentNodeDraft {
                path: "r".to_string(),
                parent_path: None,
                depth: 0,
                position: 0,
                node_type: "document".to_string(),
                title: Some("The Peninsula".to_string()),
                char_start: 0,
                char_end: md.chars().count() as i64,
                metadata: Map::new(),
            },
            DocumentNodeDraft {
                path: "r.s1".to_string(),
                parent_path: Some("r".to_string()),
                depth: 1,
                position: 0,
                node_type: "section".to_string(),
                title: Some("Ledger".to_string()),
                char_start: 18,
                char_end: md.chars().count() as i64,
                metadata: Map::new(),
            },
        ],
    )
    .await;
    assert!(struct_tree.len() > 1, "re-chunk writes structure");
    for node in &struct_tree {
        assert!(0 <= node.char_start && node.char_start <= node.char_end);
        assert!(node.char_end <= md.chars().count() as i64);
    }
    let struct_passages = insert_passages(
        pool,
        struct_doc.id,
        vec![PassageDraft {
            position: 0,
            char_start: 18,
            char_end: md.chars().count() as i64,
            locator: Map::new(),
            text: md.chars().skip(18).collect(),
            token_count: Some(8),
            chunker: "prose_window".to_string(),
            chunker_version: "2.0".to_string(),
            metadata: Map::new(),
            node_id: Some(struct_tree[1].id),
        }],
    )
    .await;
    assert_eq!(struct_passages[0].node_id, Some(struct_tree[1].id));

    for id in [doc.id, struct_doc.id] {
        cleanup_doc(pool, id).await;
    }
}

/// Backfill shapes at the repo level: textless documents are candidates, a
/// stored put retires them, and planning without writing changes nothing.
async fn scn_backfill_like_flow(pool: &PgPool) {
    let repo = PgDocumentTextRepo::new(pool.clone());
    let good = insert_doc(pool, doc_draft("backfill-good", 0xD7)).await;
    let pending = insert_doc(pool, doc_draft("backfill-pending", 0xD8)).await;
    put_text(pool, good.id, DOC_TEXT, "test", "1.0").await;

    // The plan is the missing list: pending is a candidate, good is not.
    let plan = repo.missing_document_ids(None).await.expect("plan");
    assert!(plan.contains(&pending.id));
    assert!(!plan.contains(&good.id));

    // A dry run writes nothing: the list is unchanged afterwards.
    let before: Vec<Uuid> = plan
        .iter()
        .copied()
        .filter(|id| *id == pending.id)
        .collect();
    assert_eq!(before, vec![pending.id]);
    let rerun = repo.missing_document_ids(None).await.expect("dry");
    assert!(rerun.contains(&pending.id), "planning never stores text");

    // Executing the plan retires the candidate with its parser identity.
    put_text(pool, pending.id, DOC_TEXT, "test", "1.0").await;
    assert_eq!(
        repo.get_text(pending.id).await.expect("text"),
        Some(DOC_TEXT.to_string())
    );
    assert!(!repo
        .missing_document_ids(None)
        .await
        .expect("after")
        .contains(&pending.id));
    let versions = repo.parser_versions(&[pending.id]).await.expect("versions");
    assert_eq!(versions.get(&pending.id).map(String::as_str), Some("1.0"));

    for id in [good.id, pending.id] {
        cleanup_doc(pool, id).await;
    }
}

// ---------------------------------------------------------------------------
// Driver: one ordered test, serial by construction, scratch dropped at the end
// ---------------------------------------------------------------------------

/// Driver: one ordered test, serial by construction, scratch dropped at the end.
/// Transport-failure scenario below; the driver follows it.
/// Transport failures fire every fallible step they can reach.
///
/// A closed pool fails every pool-direct method before any SQL runs, and a
/// `with_clauses`/filter call with an entity id fails inside name resolution.
/// Terminating the scratch backends fails an open transaction's next
/// statement, which covers transactional writes and `COMMIT`/`ROLLBACK`
/// themselves. Every failure maps to `Error::Storage` via `db_err`.
/// Methods are atomic from the outside, so only each method's *first*
/// fallible step fires here; later steps need statement-specific failures
/// (covered per method) or are unreachable once the earlier steps succeed.
fn must_be_storage<T>(result: Result<T, Error>) {
    match result {
        Ok(_) => panic!("unreachable database must fail"),
        Err(err) => assert_eq!(error_kind(&err), "storage", "{err:?}"),
    }
}

/// Kill every backend on the scratch database except the caller's own admin
/// session, so an open transaction's next statement fails deterministically.
async fn kill_scratch_backends(admin: &PgPool, name: &str) {
    let killed: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
         WHERE datname = $1 AND pid <> pg_backend_pid()) AS terminated",
    )
    .bind(name)
    .fetch_one(admin)
    .await
    .expect("terminate backends");
    assert!(killed >= 1, "expected live backends to terminate");
}

async fn scn_db_failures(pool: &PgPool, scratch: &Scratch) {
    let admin = PgPool::connect(&scratch.admin).await.expect("admin");
    // Closed pool: acquiring fails before any statement.
    let dead = PgPool::connect(&scratch.url).await.expect("connect");
    dead.close().await;

    let docs = PgDocumentRepo::new(dead.clone());
    must_be_storage(docs.get(Uuid::now_v7()).await);
    must_be_storage(docs.get_many(&[Uuid::now_v7()]).await);
    must_be_storage(docs.find_by_hash(&[0u8; 32], "s").await);
    must_be_storage(docs.iter_by_filter(&DocumentFilter::default()).await);
    must_be_storage(docs.count(None).await);
    must_be_storage(docs.delete(Uuid::now_v7()).await);
    must_be_storage(docs.find_by_metadata("k", "v").await);
    must_be_storage(docs.update_metadata(Uuid::now_v7(), Map::new()).await);

    let texts = PgDocumentTextRepo::new(dead.clone());
    let missing = Uuid::now_v7();
    must_be_storage(texts.get(missing).await);
    must_be_storage(texts.get_text(missing).await);
    must_be_storage(texts.parser_versions(&[missing]).await);
    must_be_storage(texts.lengths(missing).await);
    must_be_storage(texts.missing_document_ids(None).await);
    must_be_storage(texts.get_span(missing, 0, 1).await);
    must_be_storage(texts.get_spans(&[(missing, 0, 1)]).await);
    must_be_storage(texts.find_documents_containing("x", 1).await);
    must_be_storage(texts.find_raw(missing, "x").await);
    must_be_storage(texts.find_normalized(missing, "x").await);
    must_be_storage(texts.count().await);

    let passages = PgPassageRepo::new(dead.clone());
    must_be_storage(passages.get(missing).await);
    must_be_storage(passages.get_many(&[missing]).await);
    must_be_storage(passages.get_by_document(missing).await);
    must_be_storage(passages.get_context(missing, 1, 1).await);
    must_be_storage(passages.vector_search(&[1.0], "m", "v", None, 1).await);
    must_be_storage(passages.keyword_search("x", Some("english"), None, 1).await);
    must_be_storage(passages.keyword_search("x", None, None, 1).await);
    must_be_storage(passages.get_embedding(missing, "m", "v").await);
    must_be_storage(
        passages
            .filter_candidate_ids::<ExtensionClause>(&Map::new(), None)
            .await,
    );
    let mut with_entity = Map::new();
    with_entity.insert(
        "author_entity_id".to_string(),
        Value::from(Uuid::now_v7().to_string()),
    );
    must_be_storage(
        passages
            .filter_candidate_ids::<ExtensionClause>(&with_entity, None)
            .await,
    );
    must_be_storage(
        passages
            .filter_candidate_ids_with_clauses(&Map::new(), &HashMap::new())
            .await,
    );
    must_be_storage(passages.covering_span(missing, 0, 1).await);
    must_be_storage(passages.count().await);
    must_be_storage(passages.get_by_node(missing, false).await);
    must_be_storage(passages.get_by_node(missing, true).await);

    let nodes = PgDocumentNodeRepo::new(dead.clone());
    must_be_storage(nodes.get(missing).await);
    must_be_storage(nodes.get_tree(missing).await);
    must_be_storage(nodes.get_outline(missing, None).await);
    must_be_storage(nodes.get_subtree(missing).await);
    must_be_storage(nodes.get_ancestors(missing).await);
    must_be_storage(nodes.get_ancestors_many(&[missing]).await);
    must_be_storage(nodes.find_by_span(missing, 0, 1).await);
    must_be_storage(nodes.count().await);

    let spans = PgSourceSpanRepo::new(dead.clone());
    must_be_storage(spans.get(missing).await);
    must_be_storage(spans.for_document(missing).await);
    must_be_storage(spans.stale(1).await);

    let lookup = PgLemmaLookup::new(dead.clone());
    must_be_storage(lookup.verse_map_is_loaded().await);
    must_be_storage(lookup.known_books("he").await);
    must_be_storage(lookup.find(&LemmaQuery::new("4941")).await);

    // Bulk writes fail on their first statement after the kill (single-statement
    // methods fail at the execute; the transaction managers fail at `BEGIN`).
    must_be_storage(passages.set_locators(&[(Uuid::now_v7(), Map::new())]).await);
    must_be_storage(passages.set_node_ids(&[(Uuid::now_v7(), None)]).await);
    // Caller-transaction writes fail on their first statement after the kill.
    let nodes = PgDocumentNodeRepo::new(dead.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(
        passages
            .relabel_version(&mut tx, &[Uuid::now_v7()], "9.9", &HashMap::new())
            .await,
    );
    drop(tx);
    let mut tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(
        passages
            .store_embeddings(&mut tx, &[Uuid::now_v7()], &[vec![1.0]], "m", "v", 1)
            .await,
    );
    drop(tx);
    let mut tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(
        passages
            .index_fts(&mut tx, &[Uuid::now_v7()], &["x".to_string()], "english")
            .await,
    );
    drop(tx);
    let mut tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(
        nodes
            .insert_many(
                &mut tx,
                Uuid::now_v7(),
                &[DocumentNodeDraft {
                    path: "r".to_string(),
                    parent_path: None,
                    depth: 0,
                    position: 0,
                    node_type: "document".to_string(),
                    title: None,
                    char_start: 0,
                    char_end: 10,
                    metadata: Map::new(),
                }],
            )
            .await,
    );
    drop(tx);
    let mut tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(docs.insert(&mut tx, doc_draft("dead-1", 0xDD)).await);
    drop(tx);
    let mut tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(texts.put(&mut tx, missing, "x", "test", "1.0").await);
    drop(tx);
    // `COMMIT` and `ROLLBACK` themselves fail on a dead connection.
    let tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(tx.commit().await.map(|_| ()));
    let tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(tx.rollback().await.map(|_| ()));
    admin.close().await;
}

/// Fault injection the coverage bar demands be deterministic, not waived.
///
/// Every arm here fires a real Postgres failure through the public API.
/// A closed pool only ever fails a method's *first* statement, so the
/// late-`?` arms need failures *after* an earlier statement succeeded:
/// - chapter-ranged `find` binds `WhereParam::Int` (unit tests never touch
///   the database, so only this covers the `Int` bind arm);
/// - a corrupt `ref` whose chapter overflows `int4` fails the occurrences
///   `SELECT` after totals and aggregates succeeded;
/// - renaming `core.edition_books` away fails the `by_book` aggregate after
///   `by_surface` succeeded (restored in-scenario, so later scenarios keep
///   working).
async fn scn_lemma_faults(pool: &PgPool) {
    let lookup = PgLemmaLookup::new(pool.clone());
    let ranged = lookup
        .find(&LemmaQuery {
            chapter_start: Some(1),
            chapter_end: Some(150),
            ..LemmaQuery::new("4941")
        })
        .await
        .expect("chapter-ranged find");
    assert!(ranged.total > 0, "seeded 4941 must exist");

    // Corrupt reference: `split_part(ref, '.', 2)::int` overflows `int4`.
    // Totals and aggregates read no casts, so they succeed first.
    let docs = PgDocumentRepo::new(pool.clone());
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let parent = docs
        .insert(&mut tx, doc_draft("fault-corrupt-ref", 0xF0))
        .await
        .expect("corrupt parent");
    tx.commit().await.expect("commit");
    sqlx::query(
        "INSERT INTO core.words (document_id, position, char_start, char_end, \
         surface, lemma, strong, morph, language, ref, from_qere) \
         VALUES ($1, 0, 0, 1, 'x', 'l', '99999', 'm', 'he', 'Gen.9999999999.1', false)",
    )
    .bind(parent.id)
    .execute(pool)
    .await
    .expect("corrupt row");
    must_be_storage(lookup.find(&LemmaQuery::new("99999")).await);
    // The corrupt row leaves with its parent (words cascade).
    docs.delete(parent.id).await.expect("delete corrupt parent");

    // Missing `edition_books`: totals read `words` alone, `by_book` joins.
    sqlx::query("ALTER TABLE core.edition_books RENAME TO edition_books_hidden")
        .execute(pool)
        .await
        .expect("hide edition_books");
    must_be_storage(lookup.find(&LemmaQuery::new("4941")).await);
    sqlx::query("ALTER TABLE core.edition_books_hidden RENAME TO edition_books")
        .execute(pool)
        .await
        .expect("restore edition_books");
    // Restored: ordinal ordering works again.
    assert!(!lookup
        .known_books("he")
        .await
        .expect("books after restore")
        .is_empty());
}

/// `PgTx::begin` fails deterministically on a poisoned pooled connection.
///
/// A closed pool fails the *acquire*; this fails the `BEGIN` execute: abort
/// a transaction with a unique violation on `(content_hash, source)`, drop
/// it without rollback (manually-managed connections return to the pool
/// still inside the aborted transaction — `sqlx` does not reset them on
/// release), and the next `begin` reuses that same connection
/// (`max_connections(1)`), where `BEGIN` answers "current transaction is
/// aborted". Nothing commits, so the isolation check at the end still holds.
async fn scn_tx_begin_on_aborted(scratch: &Scratch) {
    let solo = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&scratch.url)
        .await
        .expect("solo pool");
    let docs = PgDocumentRepo::new(solo.clone());
    let mut tx = PgTx::begin(&solo).await.expect("begin");
    let draft = doc_draft("abort-poison", 0xAB);
    docs.insert(&mut tx, draft.clone())
        .await
        .expect("first insert");
    // Deliberate unique violation: aborts the transaction.
    must_be_storage(docs.insert(&mut tx, draft).await);
    drop(tx);
    // Same connection, still aborted.
    must_be_storage(PgTx::begin(&solo).await.map(|_| ()));
    solo.close().await;
}
/// Fail loudly if any backend on the scratch database sits in an aborted
/// transaction: a leaked aborted connection poisons the pool for later
/// scenarios (`25P02` far from the crime). Called between scenarios to
/// pinpoint the leaker instead of debugging the victim.
async fn assert_no_aborted_backends(pool: &PgPool, db: &str, location: &str) {
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = $1 AND state LIKE '%aborted%'",
    )
    .bind(db)
    .fetch_one(pool)
    .await
    .expect("aborted check");
    assert_eq!(
        count, 0,
        "aborted transaction leaked into the pool ({location})"
    );
}

/// Hide a table around one call, so the enclosed statement fails on the
/// missing table after earlier statements succeeded. Deterministic: the
/// failure is structural (undefined table), not timed.
async fn hide_table(pool: &PgPool, table: &str) -> String {
    let (schema, name) = table.split_once('.').expect("schema-qualified table");
    let hidden = format!("{name}_hidden_cov");
    sqlx::query(&format!("ALTER TABLE {schema}.{name} RENAME TO {hidden}"))
        .execute(pool)
        .await
        .expect("hide table for fault test");
    hidden
}

async fn restore_table(pool: &PgPool, table: &str, hidden: &str) {
    let (schema, name) = table.split_once('.').expect("schema-qualified table");
    sqlx::query(&format!("ALTER TABLE {schema}.{hidden} RENAME TO {name}"))
        .execute(pool)
        .await
        .expect("restore table after fault test");
}

/// Terminate the one backend blocked on a lock running `expect_sql`.
/// Deterministic pinning: the method cannot proceed past the held lock, so
/// the kill always lands in the intended statement — never in a microsecond
/// gap. Panics loudly if the method did not block as designed.
async fn kill_blocked_backend(admin: &PgPool, db: &str, expect_sql: &str) {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    loop {
        let rows: Vec<(i32, Option<String>, Option<String>)> = sqlx::query_as(
            "SELECT pid, wait_event_type, query FROM pg_stat_activity WHERE datname = $1",
        )
        .bind(db)
        .fetch_all(admin)
        .await
        .expect("read activity");
        let blocked: Vec<i32> = rows
            .iter()
            .filter(|(_, wait, query)| {
                wait.as_deref() == Some("Lock")
                    && query.as_deref().is_some_and(|q| q.contains(expect_sql))
            })
            .map(|(pid, _, _)| *pid)
            .collect();
        if blocked.len() == 1 {
            sqlx::query("SELECT pg_terminate_backend($1)")
                .bind(blocked[0])
                .execute(admin)
                .await
                .expect("terminate blocked backend");
            return;
        }
        if std::time::Instant::now() > deadline {
            panic!("backend did not block on {expect_sql} as designed: {rows:?}");
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
}

/// Late-`?` faults across repos: each fails a statement *after* earlier
/// ones succeeded (a closed pool only ever fails the first). Table renames
/// fail the first statement touching the renamed table; the duplicate
/// insert trips the unique constraint; the missing map rows fail the
/// aggregates that join them.
async fn scn_repo_faults(pool: &PgPool, scratch: &Scratch) {
    // Span resolve: fast path, merged slice read, overlap, insert — one
    // fault per boundary.
    let spans = PgSourceSpanRepo::new(pool.clone());
    let admin = PgPool::connect(&scratch.admin).await.expect("admin");
    // Fast-path fetch fails on a killed transaction connection (held, so no
    // transparent reconnect): covers the first boundary.
    let mut tx = PgTx::begin(pool).await.expect("begin");
    kill_scratch_backends(&admin, &scratch.name).await;
    must_be_storage(spans.resolve(&mut tx, Uuid::now_v7(), 0, 1).await);
    drop(tx);
    let dead = PgPool::connect(&scratch.url).await.expect("connect");
    dead.close().await;
    let doc = insert_doc(pool, doc_draft("fault-span", 0xF5)).await;
    put_text(pool, doc.id, "hello world fault span", "test", "1.0").await;
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let hidden = hide_table(pool, "core.document_texts").await;
    must_be_storage(spans.resolve(&mut tx, doc.id, 0, 5).await);
    restore_table(pool, "core.document_texts", &hidden).await;
    // The failed resolve aborted the transaction: roll back before release,
    // or the next borrower inherits the aborted state.
    let _ = tx.rollback().await;
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let hidden = hide_table(pool, "core.passages").await;
    must_be_storage(spans.resolve(&mut tx, doc.id, 0, 5).await);
    restore_table(pool, "core.passages", &hidden).await;
    let _ = tx.rollback().await;
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let hidden = hide_table(pool, "evidence.source_spans").await;
    must_be_storage(spans.resolve(&mut tx, doc.id, 0, 5).await);
    restore_table(pool, "evidence.source_spans", &hidden).await;
    let _ = tx.rollback().await;
    // Restored: resolving works again on the same coordinates.
    let mut tx = PgTx::begin(pool).await.expect("begin");
    let span = spans
        .resolve(&mut tx, doc.id, 0, 5)
        .await
        .expect("resolve after restore");
    tx.commit().await.expect("commit");
    assert_eq!(span.quoted_text, "hello");
    cleanup_doc(pool, doc.id).await;
    dead.close().await;

    // Passage insert: the duplicate trips the unique constraint on the
    // second iteration, after the first row landed.
    let passages = PgPassageRepo::new(pool.clone());
    let dup_doc = insert_doc(pool, doc_draft("fault-dup", 0xD0)).await;
    let drafts = vec![passage_draft(0, "dup row"), passage_draft(0, "dup row")];
    let mut tx = PgTx::begin(pool).await.expect("begin");
    must_be_storage(passages.insert_many(&mut tx, dup_doc.id, drafts).await);
    let _ = tx.rollback().await;
    cleanup_doc(pool, dup_doc.id).await;
    assert_no_aborted_backends(pool, &scratch.name, "after span+dup faults").await;

    // Candidate execution: no tables touched before the candidate SELECT.
    let hidden = hide_table(pool, "core.passages").await;
    must_be_storage(
        passages
            .filter_candidate_ids_with_clauses(&Map::new(), &HashMap::new())
            .await,
    );
    restore_table(pool, "core.passages", &hidden).await;
    // Malformed filters fail in parsing, before any database use; unknown
    // extension ids pass parsing and fail in validation (a different site).
    let mut bad = Map::new();
    bad.insert("extensions".to_string(), Value::String("nope".to_string()));
    assert!(passages
        .filter_candidate_ids_with_clauses(&bad, &HashMap::new())
        .await
        .is_err());
    assert!(passages
        .filter_candidate_ids::<ExtensionClause>(&bad, None)
        .await
        .is_err());
    let mut unknown_ext = Map::new();
    let mut ext_map = Map::new();
    ext_map.insert("nope".to_string(), Value::Null);
    unknown_ext.insert("extensions".to_string(), Value::Object(ext_map));
    assert!(passages
        .filter_candidate_ids_with_clauses(&unknown_ext, &HashMap::new())
        .await
        .is_err());
    // Unknown keys fail the same validation with the other variant (both
    // arms must fire in this binary too, not just in unit tests).
    let mut unknown_key = Map::new();
    unknown_key.insert("bogus".to_string(), Value::Null);
    assert!(passages
        .filter_candidate_ids_with_clauses(&unknown_key, &HashMap::new())
        .await
        .is_err());
    // Every keyed extractor needs its own fault in this binary too, not
    // just in unit tests (per-instantiation regions): each refuses loudly
    // through the public API.
    for (key, value) in [
        ("document_types", Value::Number(42.into())),
        ("mentions_entity_ids", Value::Number(42.into())),
        ("date_range_start", Value::String("not-a-date".to_string())),
        ("date_range_start", Value::Number(42.into())),
        ("date_range_end", Value::String("not-a-date".to_string())),
        ("language", Value::Number(42.into())),
        ("author_entity_id", Value::String("not-a-uuid".to_string())),
        (
            "recipient_entity_id",
            Value::String("not-a-uuid".to_string()),
        ),
    ] {
        let mut filters = Map::new();
        filters.insert(key.to_string(), value);
        assert!(
            passages
                .filter_candidate_ids_with_clauses(&filters, &HashMap::new())
                .await
                .is_err(),
            "{key}"
        );
    }
    // naming the opaque clause type (never a silently unfiltered query).
    let mut registry = HashMap::new();
    registry.insert(
        "nope".to_string(),
        ExtensionClause {
            sql: "SELECT 1".to_string(),
            params: Vec::new(),
        },
    );
    let refused = passages
        .filter_candidate_ids::<ExtensionClause>(&unknown_ext, Some(&registry))
        .await;
    assert_eq!(error_kind(&refused.unwrap_err()), "plugin");
    // Entity-name resolution: author-only and recipient-only fail their own
    // query on a dead pool (the `None` side short-circuits without one).
    let dead_passages = PgPassageRepo::new(dead.clone());
    let mut author_only = Map::new();
    author_only.insert(
        "author_entity_id".to_string(),
        Value::from(Uuid::now_v7().to_string()),
    );
    must_be_storage(
        dead_passages
            .filter_candidate_ids::<ExtensionClause>(&author_only, None)
            .await,
    );
    let mut recipient_only = Map::new();
    recipient_only.insert(
        "recipient_entity_id".to_string(),
        Value::from(Uuid::now_v7().to_string()),
    );
    must_be_storage(
        dead_passages
            .filter_candidate_ids::<ExtensionClause>(&recipient_only, None)
            .await,
    );
    // Same pair through `with_clauses`: its own resolve sites need their own
    // faults (different `?` locations from the generic method's).
    must_be_storage(
        dead_passages
            .filter_candidate_ids_with_clauses(&author_only, &HashMap::new())
            .await,
    );
    must_be_storage(
        dead_passages
            .filter_candidate_ids_with_clauses(&recipient_only, &HashMap::new())
            .await,
    );
    assert_no_aborted_backends(pool, &scratch.name, "after entity faults").await;
    // Empty verse map withholds every English reference (the map-loaded
    // note path); the edition_books fault in `scn_lemma_faults` covers the
    // aggregates boundary.
    let lookup = PgLemmaLookup::new(pool.clone());
    sqlx::query("CREATE TABLE core.verse_map_backup AS TABLE core.verse_map")
        .execute(pool)
        .await
        .expect("back up verse_map");
    sqlx::query("DELETE FROM core.verse_map")
        .execute(pool)
        .await
        .expect("empty map");
    let unmapped = lookup
        .find(&LemmaQuery::new("4941"))
        .await
        .expect("unmapped find");
    assert!(!unmapped.occurrences.is_empty());
    assert!(unmapped
        .occurrences
        .iter()
        .all(|o| o.english.mapping == Mapping::Unmapped));
    assert!(unmapped
        .notes
        .iter()
        .any(|n| n.contains("verse_map is empty")));
    sqlx::query("INSERT INTO core.verse_map SELECT * FROM core.verse_map_backup")
        .execute(pool)
        .await
        .expect("restore map");
    sqlx::query("DROP TABLE core.verse_map_backup")
        .execute(pool)
        .await
        .expect("drop backup");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM core.verse_map")
        .fetch_one(pool)
        .await
        .expect("map count");
    assert_eq!(count, 1978);
    assert_no_aborted_backends(pool, &scratch.name, "after verse_map faults").await;
    let hidden = hide_table(pool, "core.verse_map").await;
    must_be_storage(lookup.find(&LemmaQuery::new("4941")).await);
    restore_table(pool, "core.verse_map", &hidden).await;
    assert!(lookup.verse_map_is_loaded().await.expect("map back"));

    // Vector ef path: acquire, BEGIN, SET, SELECT — one fault per boundary
    // (the COMMIT tail needs none). Acquire fails on a dead pool.
    let dead_ef = PgPassageRepo::with_ef_search(dead.clone(), 16);
    must_be_storage(dead_ef.vector_search(&[1.0], "m", "v", None, 1).await);
    // Poisoned pooled connection for BEGIN: an aborted transaction returned
    // without rollback, reused next.
    let solo = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&scratch.url)
        .await
        .expect("solo pool");
    let ef = PgPassageRepo::with_ef_search(solo.clone(), 16);
    {
        let mut conn = solo.acquire().await.expect("acquire");
        sqlx::query("BEGIN")
            .execute(&mut *conn)
            .await
            .expect("begin");
        sqlx::query("SELECT 1/0")
            .execute(&mut *conn)
            .await
            .expect_err("abort tx");
    }
    must_be_storage(ef.vector_search(&[1.0], "m", "v", None, 1).await);
    // pgvector accepts any integer breadth (probed: even `-5` sets), so a
    // negative breadth still searches — `SET LOCAL` is best-effort and the
    // `SELECT` below diagnoses dead connections instead.
    let ef_neg = PgPassageRepo::with_ef_search(pool.clone(), -5);
    ef_neg
        .vector_search(&[1.0], "m", "v", None, 1)
        .await
        .expect("negative ef searches");
    // Missing embeddings table fails the SELECT after BEGIN+SET succeeded
    // (a fresh pool: `solo` above is poisoned on purpose and would fail at
    // BEGIN instead, covering the wrong site).
    let ef_fresh = PgPassageRepo::with_ef_search(pool.clone(), 16);
    let hidden = hide_table(pool, "core.passage_embeddings").await;
    must_be_storage(ef_fresh.vector_search(&[1.0], "m", "v", None, 1).await);
    restore_table(pool, "core.passage_embeddings", &hidden).await;
    solo.close().await;
}

/// `update_metadata` faults: the fetch fails on a missing table, a scalar
/// `metadata` merges as `{}` instead of crashing, and the write fails while
/// blocked on a lock — pinned by `wait_event`, never by timing.
async fn scn_update_metadata_faults(pool: &PgPool, scratch: &Scratch) {
    let docs = PgDocumentRepo::new(pool.clone());
    let hidden = hide_table(pool, "core.documents").await;
    must_be_storage(docs.update_metadata(Uuid::now_v7(), Map::new()).await);
    restore_table(pool, "core.documents", &hidden).await;

    let doc = insert_doc(pool, doc_draft("fault-meta", 0xFA)).await;
    // Scalar metadata (schema drift or a manual write — the column is NOT
    // NULL-able, so `NULL` cannot occur) merges as `{}` instead of crashing:
    // the Python `row.metadata or {}` keeps a truthy scalar and then fails
    // unpacking it, which no writer can produce either.
    sqlx::query("UPDATE core.documents SET metadata = '[1]' WHERE id = $1")
        .bind(doc.id)
        .execute(pool)
        .await
        .expect("scalar metadata");
    let mut patch = Map::new();
    patch.insert("k".to_string(), Value::from("v"));
    let updated = docs
        .update_metadata(doc.id, patch)
        .await
        .expect("scalar merges");
    assert_eq!(updated.metadata.get("k"), Some(&Value::from("v")));

    // Write fault: hold the row lock, so the UPDATE blocks; terminate the
    // blocked backend (pinned by statement text), proving fail-loud under
    // contention rather than a hang.
    let admin = PgPool::connect(&scratch.admin).await.expect("admin");
    let mut lock_conn = pool.acquire().await.expect("lock conn");
    sqlx::query("BEGIN")
        .execute(&mut *lock_conn)
        .await
        .expect("begin lock");
    sqlx::query("SELECT id FROM core.documents WHERE id = $1 FOR UPDATE")
        .bind(doc.id)
        .execute(&mut *lock_conn)
        .await
        .expect("take lock");
    let task_pool = pool.clone();
    let task_doc = doc.id;
    let handle = tokio::spawn(async move {
        let repo = PgDocumentRepo::new(task_pool);
        repo.update_metadata(task_doc, Map::new()).await.map(|_| ())
    });
    kill_blocked_backend(&admin, &scratch.name, "UPDATE core.documents SET metadata").await;
    must_be_storage(handle.await.expect("join"));
    sqlx::query("ROLLBACK")
        .execute(&mut *lock_conn)
        .await
        .expect("release lock");
    admin.close().await;
    cleanup_doc(pool, doc.id).await;
}

#[tokio::test]
async fn pg_repos() {
    let scratch = create_scratch().await;
    let pool = &scratch.pool;

    let seeded: i64 = sqlx::query_scalar("SELECT count(*) FROM core.words")
        .fetch_one(pool)
        .await
        .expect("seeded words");
    println!("scratch {0} holds {seeded} seeded words", scratch.name);
    assert!(
        seeded > 10_000,
        "reference rows must be seeded, got {seeded}"
    );

    scn_transactions(pool).await;
    scn_documents_crud(pool).await;
    scn_documents_filters(pool).await;
    scn_documents_not_found_and_restrict(pool).await;
    scn_texts_round_trip(pool).await;
    scn_spans_resolve(pool).await;
    scn_nodes_tree(pool).await;
    scn_passages_crud_and_windows(pool).await;
    scn_vector_search_exact(pool).await;
    scn_keyword_multilingual(pool).await;
    scn_filter_candidates(pool).await;
    scn_lemma_survey(pool, &scratch).await;
    scn_versification_reference(pool).await;
    scn_reindex_like_flow(pool).await;
    scn_backfill_like_flow(pool).await;
    assert_no_aborted_backends(pool, &scratch.name, "after backfill").await;
    scn_lemma_faults(pool).await;
    assert_no_aborted_backends(pool, &scratch.name, "after lemma_faults").await;
    scn_repo_faults(pool, &scratch).await;
    assert_no_aborted_backends(pool, &scratch.name, "after repo_faults").await;
    scn_update_metadata_faults(pool, &scratch).await;
    scn_tx_begin_on_aborted(&scratch).await;
    scn_db_failures(pool, &scratch).await;
    // Isolation: every scenario cleaned up, so only the seeded reference
    // rows remain.
    let orphans: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM core.documents WHERE id NOT IN \
         (SELECT document_id FROM core.words)",
    )
    .fetch_one(pool)
    .await
    .expect("orphan check");
    assert_eq!(orphans, 0, "scenarios must delete what they created");

    drop_scratch(scratch).await;
}
