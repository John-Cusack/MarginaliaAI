//! Render a work's body with footnotes derived from the corpus.
//!
//! Python source: `services/works/render.py`. Display strings are built here
//! and only here: author, title and year come from document metadata, the
//! tier from verification, and nothing is ever written back to the file.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use marginalia_types::documents::Document;
use marginalia_types::ports::DocumentRepo;
use marginalia_types::works_files::CitationEntry;
use marginalia_types::works_ports::VerifyPort;
use marginalia_types::{Error, Result};

use crate::files::{strip_definition_lines, WorkFileReader};

/// Locator keys with a conventional rendering. Anything else renders as
/// `key value`, so a pack-specific locator still reads sensibly.
static LOCATOR_WORDS: &[(&str, &str)] = &[
    ("volume", "vol."),
    ("page", "p."),
    ("chapter", "ch."),
    ("verse", "v."),
];

/// One rendered footnote: `[^id]: text` plus its structured fields.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Footnote {
    pub id: String,
    pub text: String,
    pub provisional: bool,
    pub tier: String,
}

/// `render` output: the body with footnote definitions plus the notes.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RenderedWork {
    pub rendered: String,
    pub footnotes: Vec<Footnote>,
}

/// A year from a metadata `year` or `date` value.
///
/// Dates arrive as full ISO strings; the footnote wants the year. Four
/// leading digits are the year, anything else passes through untouched so a
/// non-date never silently becomes one.
pub fn footnote_year(value: Option<&Value>) -> Option<String> {
    let value = value?;
    // Python `str(value)`: plain strings stay bare, ints stringify, and
    // bools spell `True`/`False` — matched here so a boolean year renders
    // exactly as Python would rather than as JSON `true`/`false`.
    let text = match value {
        Value::Null => return None,
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) => number.to_string(),
        Value::Array(_) | Value::Object(_) => {
            serde_json::to_string(value).expect("JSON value serializes")
        }
    };
    // `None` cannot arrive here (see above): Python's `metadata.get` yields
    // `None` for an explicit null, which the `?` already returned as `None`.
    let text = text.trim().to_owned();
    let leading: String = text.chars().take(4).collect();
    if leading.len() == 4 && leading.chars().all(|ch| ch.is_ascii_digit()) {
        let fifth = text.chars().nth(4);
        if fifth.is_none_or(|ch| !ch.is_ascii_digit()) {
            return Some(leading);
        }
    }
    if text.is_empty() {
        None
    } else {
        Some(text)
    }
}

/// Display a metadata or locator scalar the way an f-string would: bare
/// strings stay bare, ints stringify, bools spell `True`/`False`.
fn display_scalar(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) => number.to_string(),
        Value::Array(_) | Value::Object(_) => {
            serde_json::to_string(value).expect("JSON value serializes")
        }
    }
}

/// The footnote display string for one entry: `author, *title*, edition,
/// vol./year/locators. [tier]` plus ` [provisional]` when any part is
/// missing. Pure over the entry, its document row, and the tier string.
pub fn build_footnote(entry: &CitationEntry, document: Option<&Document>, tier: &str) -> Footnote {
    let mut metadata = document.map(|doc| doc.metadata.clone()).unwrap_or_default();
    if let Some(doc) = document {
        // Python checks `document.title` for truthiness: an empty title does
        // not backfill, and a metadata title always wins.
        if doc.title.as_ref().is_some_and(|title| !title.is_empty())
            && !metadata.contains_key("title")
        {
            metadata.insert(
                "title".to_owned(),
                Value::String(doc.title.clone().expect("title is some")),
            );
        }
    }

    // An explicit JSON null is Python's missing `None`, not a value.
    let author = metadata.get("author").filter(|value| !value.is_null());
    let title = metadata.get("title").filter(|value| !value.is_null());
    let year = footnote_year(
        metadata
            .get("year")
            .or_else(|| metadata.get("date"))
            .filter(|value| !value.is_null()),
    );
    let provisional = author.is_none() || title.is_none() || year.is_none();

    let shown_author =
        author.map_or_else(|| format!("document {}", entry.document_id), display_scalar);
    let shown_title = title.map_or_else(
        || format!("document {}", entry.document_id),
        |value| format!("*{}*", display_scalar(value)),
    );
    let mut text = format!("{shown_author}, {shown_title}");
    if let Some(edition) = entry.edition.as_ref() {
        text.push_str(&format!(", {edition}"));
    }
    // `volume` renders right after the edition (`vol. II`), ahead of the
    // year; every other locator follows the year in insertion order, exactly
    // like Python's `dict.pop("volume", None)` plus insertion-ordered
    // iteration. (`Map::remove` is `swap_remove` under `preserve_order` and
    // would disorder the survivors, so volume is read, not removed.)
    if let Some(volume) = entry.locator.get("volume") {
        text.push_str(&format!(", vol. {}", display_scalar(volume)));
    }
    if let Some(year) = year {
        text.push_str(&format!(" ({year})"));
    }
    for (key, value) in entry
        .locator
        .iter()
        .filter(|(key, _)| key.as_str() != "volume")
    {
        let word = LOCATOR_WORDS
            .iter()
            .find(|(name, _)| *name == key)
            .map(|(_, word)| *word)
            .unwrap_or(key);
        text.push_str(&format!(", {word} {}", display_scalar(value)));
    }

    text.push_str(&format!(". [{tier}]"));
    if provisional {
        text.push_str(" [provisional]");
    }
    Footnote {
        id: entry.id.clone(),
        text,
        provisional,
        tier: tier.to_owned(),
    }
}

pub struct WorkRenderer<D, V> {
    documents: D,
    verification: V,
    reader: WorkFileReader,
}

impl<D, V> WorkRenderer<D, V>
where
    D: DocumentRepo,
    V: VerifyPort,
{
    pub fn new(documents: D, verification: V, works_dir: PathBuf) -> Self {
        Self {
            documents,
            verification,
            reader: WorkFileReader::new(works_dir),
        }
    }

    pub async fn render(&self, work_path: &str) -> Result<RenderedWork> {
        let work = self.reader.read(work_path).map_err(Error::from)?;
        let mut entries = work.front_matter.citations.clone();
        // Numeric-handle order: `c10` sorts after `c2`, like Python's
        // `int(entry.id[1:])` key. Handles are validated `cN` on the way in;
        // anything else sorts on its full text rather than failing.
        entries.sort_by(|a, b| {
            crate::cmp_numeric_strings(
                a.id.strip_prefix('c').unwrap_or(&a.id),
                b.id.strip_prefix('c').unwrap_or(&b.id),
            )
        });
        let mut footnotes = Vec::with_capacity(entries.len());
        for entry in &entries {
            footnotes.push(self.footnote(entry).await?);
        }
        let body = strip_definition_lines(&work.body);
        let rendered = if footnotes.is_empty() {
            work.body.clone()
        } else {
            let definitions = footnotes
                .iter()
                .map(|note| format!("[^{id}]: {text}", id = note.id, text = note.text))
                .collect::<Vec<_>>()
                .join("\n");
            format!("{}\n\n{definitions}\n", body.trim_end())
        };
        Ok(RenderedWork {
            rendered,
            footnotes,
        })
    }

    async fn footnote(&self, entry: &CitationEntry) -> Result<Footnote> {
        let document = self.documents.get(entry.document_id).await?;
        let outcome = self
            .verification
            .verify(&entry.quoted_text, Some(entry.document_id), None)
            .await?;
        Ok(build_footnote(
            entry,
            document.as_ref(),
            outcome.tier.as_str(),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::Mutex;
    use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};

    use chrono::{DateTime, Utc};
    use marginalia_types::works_ports::{VerifyResult, VerifyTier};
    use serde_json::json;
    use uuid::Uuid;

    /// Minimal executor: the fakes below never pend, so a spin poll drives
    /// them to readiness. (`tokio` is not a dependency of this crate, and
    /// `Cargo.toml` is owned by another slice.)
    fn block_on<F: Future>(mut future: F) -> F::Output {
        fn raw() -> RawWaker {
            fn noop(_: *const ()) {}
            fn clone(ptr: *const ()) -> RawWaker {
                raw_waker(ptr)
            }
            fn raw_waker(ptr: *const ()) -> RawWaker {
                RawWaker::new(ptr, &RawWakerVTable::new(clone, noop, noop, noop))
            }
            raw_waker(std::ptr::null())
        }
        // SAFETY: the waker never dereferences its null data pointer; the
        // futures polled here never clone, wake, or drop through it.
        let waker = unsafe { Waker::from_raw(raw()) };
        let mut context = Context::from_waker(&waker);
        // SAFETY: `future` is stack-owned and never moved after pinning.
        let mut pinned = unsafe { Pin::new_unchecked(&mut future) };
        loop {
            match pinned.as_mut().poll(&mut context) {
                Poll::Ready(value) => return value,
                Poll::Pending => std::thread::yield_now(),
            }
        }
    }

    struct FakeDocuments {
        docs: Mutex<HashMap<Uuid, Document>>,
        fail: bool,
    }

    fn doc(metadata: serde_json::Map<String, Value>, title: Option<&str>) -> Document {
        Document {
            id: Uuid::new_v4(),
            title: title.map(str::to_owned),
            document_type: "generic".to_owned(),
            language: None,
            source: "test".to_owned(),
            content_hash: vec![],
            parser: "test".to_owned(),
            parser_version: "0".to_owned(),
            ingested_at: DateTime::<Utc>::MIN_UTC,
            created_date_start: None,
            created_date_end: None,
            created_precision: None,
            edition_id: None,
            metadata,
        }
    }

    impl DocumentRepo for FakeDocuments {
        type Tx = ();

        async fn insert(
            &self,
            _tx: &mut Self::Tx,
            draft: marginalia_types::documents::DocumentDraft,
        ) -> Result<Document> {
            let document = Document {
                id: Uuid::new_v4(),
                title: draft.title,
                document_type: draft.document_type,
                language: draft.language,
                source: draft.source,
                content_hash: draft.content_hash,
                parser: draft.parser,
                parser_version: draft.parser_version,
                ingested_at: chrono::Utc::now(),
                created_date_start: draft.created_date_start,
                created_date_end: draft.created_date_end,
                created_precision: draft.created_precision,
                edition_id: draft.edition_id,
                metadata: draft.metadata,
            };
            self.docs
                .lock()
                .expect("fake lock")
                .insert(document.id, document.clone());
            Ok(document)
        }

        async fn get(&self, doc_id: Uuid) -> Result<Option<Document>> {
            if self.fail {
                return Err(marginalia_types::Error::Storage("docs broke".to_owned()));
            }
            Ok(self.docs.lock().expect("fake lock").get(&doc_id).cloned())
        }

        async fn get_many(&self, doc_ids: &[Uuid]) -> Result<Vec<Document>> {
            let docs = self.docs.lock().expect("fake lock");
            Ok(doc_ids
                .iter()
                .filter_map(|id| docs.get(id).cloned())
                .collect())
        }

        async fn find_by_hash(
            &self,
            content_hash: &[u8],
            source: &str,
        ) -> Result<Option<Document>> {
            Ok(self
                .docs
                .lock()
                .expect("fake lock")
                .values()
                .find(|doc| doc.content_hash == content_hash && doc.source == source)
                .cloned())
        }
        async fn find_by_edition_id(
            &self,
            _tx: &mut Self::Tx,
            edition_id: Uuid,
        ) -> Result<Option<Document>> {
            Ok(self
                .docs
                .lock()
                .expect("fake lock")
                .values()
                .find(|doc| doc.edition_id == Some(edition_id))
                .cloned())
        }

        async fn update_metadata(
            &self,
            doc_id: Uuid,
            patch: serde_json::Map<String, Value>,
        ) -> Result<Document> {
            let mut docs = self.docs.lock().expect("fake lock");
            let Some(document) = docs.get_mut(&doc_id) else {
                return Err(marginalia_types::Error::NotFound {
                    kind: "document",
                    id: doc_id.to_string(),
                });
            };
            document.metadata.extend(patch);
            Ok(document.clone())
        }

        async fn iter_by_filter(
            &self,
            _filter: &marginalia_types::documents::DocumentFilter,
        ) -> Result<Vec<Document>> {
            // The fake holds one corpus and no index: every stored row matches.
            Ok(self
                .docs
                .lock()
                .expect("fake lock")
                .values()
                .cloned()
                .collect())
        }

        async fn count(
            &self,
            _filter: Option<&marginalia_types::documents::DocumentFilter>,
        ) -> Result<i64> {
            Ok(self.docs.lock().expect("fake lock").len() as i64)
        }

        async fn delete(&self, doc_id: Uuid) -> Result<()> {
            self.docs.lock().expect("fake lock").remove(&doc_id);
            Ok(())
        }
    }
    struct FakeVerification {
        tier: VerifyTier,
        fail: bool,
    }

    impl VerifyPort for FakeVerification {
        async fn verify(
            &self,
            _quote: &str,
            _document_id: Option<Uuid>,
            _window: Option<(i64, i64)>,
        ) -> Result<VerifyResult> {
            if self.fail {
                return Err(marginalia_types::Error::Storage("verify broke".to_owned()));
            }
            Ok(VerifyResult {
                tier: self.tier,
                location: None,
                matched_fraction: None,
                divergence: None,
            })
        }
    }

    const DOC: &str = "11111111-1111-1111-1111-111111111111";

    fn entry_text(cid: &str, doc: &str) -> String {
        format!(
            "  - id: {cid}\n\
             \x20   document_id: {doc}\n\
             \x20   char_start: 34\n\
             \x20   char_end: 62\n\
             \x20   quoted_text: \"The prophets pair two words.\"\n\
             \x20   intent: quotation\n\
             \x20   edition_key: DABAR_2026\n\
             \x20   locator: {{volume: II, page: 64}}\n"
        )
    }

    fn file_text(entries: &str, body: &str) -> String {
        format!(
            "---\n\
             work: W-001\n\
             title: \"A fragment\"\n\
             type: essay\n\
             status: draft\n\
             created: 2026-09-04\n\
             claims: []\n\
             citations:\n\
             {entries}---\n\n{body}"
        )
    }

    fn renderer(
        docs: HashMap<Uuid, Document>,
        dir: &std::path::Path,
    ) -> WorkRenderer<FakeDocuments, FakeVerification> {
        renderer_with(docs, dir, false, false)
    }

    fn renderer_with(
        docs: HashMap<Uuid, Document>,
        dir: &std::path::Path,
        fail_docs: bool,
        fail_verify: bool,
    ) -> WorkRenderer<FakeDocuments, FakeVerification> {
        WorkRenderer::new(
            FakeDocuments {
                docs: Mutex::new(docs),
                fail: fail_docs,
            },
            FakeVerification {
                tier: VerifyTier::Normalized,
                fail: fail_verify,
            },
            dir.to_path_buf(),
        )
    }

    fn metadata_doc(metadata: Value, title: Option<&str>, id: Uuid) -> (Uuid, Document) {
        let mut document = doc(
            metadata.as_object().expect("metadata object").clone(),
            title,
        );
        document.id = id;
        (id, document)
    }

    #[test]
    fn test_footnote_year_first_four_digits_rule() {
        assert_eq!(footnote_year(None), None);
        assert_eq!(footnote_year(Some(&json!("1964"))), Some("1964".to_owned()));
        assert_eq!(
            footnote_year(Some(&json!("2026-09-04"))),
            Some("2026".to_owned())
        );
        assert_eq!(footnote_year(Some(&json!(1964))), Some("1964".to_owned()));
        // Five leading digits are not a year prefix.
        assert_eq!(
            footnote_year(Some(&json!("19645"))),
            Some("19645".to_owned())
        );
        assert_eq!(footnote_year(Some(&json!(""))), None);
        assert_eq!(footnote_year(Some(&json!("  "))), None);
        // Non-date text passes through untouched.
        assert_eq!(
            footnote_year(Some(&json!("circa 1964"))),
            Some("circa 1964".to_owned())
        );
    }

    #[test]
    fn test_full_metadata_renders_the_guide_example() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entry_text("c1", DOC), "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Kittel", "title": "Theological Dictionary of the New Testament", "year": "1964"}),
            Some("Dabaris"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");

        assert_eq!(
            result.footnotes,
            vec![Footnote {
                id: "c1".to_owned(),
                text: "Kittel, *Theological Dictionary of the New Testament*, vol. II (1964), p. 64. [normalized]".to_owned(),
                provisional: false,
                tier: "normalized".to_owned(),
            }]
        );
        assert!(
            result.rendered.ends_with(
                "[^c1]: Kittel, *Theological Dictionary of the New Testament*, vol. II (1964), p. 64. [normalized]\n"
            ),
            "{}",
            result.rendered
        );
    }

    #[test]
    fn test_surviving_locators_keep_insertion_order() {
        // `works/mishpat-tsedaqah-survey.md` carries `{entry, article}`
        // locators: insertion order, not sort order, is the contract.
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        let entries = entry_text("c1", DOC).replace(
            "locator: {volume: II, page: 64}",
            "locator: {volume: II, entry: 5, article: 12}",
        );
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entries, "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Kittel", "title": "Theological Dictionary of the New Testament", "year": "1964"}),
            Some("Dabaris"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");
        let [footnote] = result.footnotes.try_into().expect("one footnote");
        assert_eq!(
            footnote.text,
            "Kittel, *Theological Dictionary of the New Testament*, vol. II (1964), entry 5, article 12. [normalized]"
        );
    }

    #[test]
    fn test_missing_author_is_provisional() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entry_text("c1", DOC), "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"title": "Dabaris", "year": "2020"}),
            Some("Dabaris"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");
        let [footnote] = result.footnotes.try_into().expect("one footnote");
        let footnote: &Footnote = &footnote;

        assert!(footnote.provisional);
        assert!(
            footnote
                .text
                .starts_with(&format!("document {DOC}, *Dabaris*")),
            "{}",
            footnote.text
        );
        assert!(
            footnote.text.ends_with("[normalized] [provisional]"),
            "{}",
            footnote.text
        );
    }

    #[test]
    fn test_missing_year_is_provisional() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entry_text("c1", DOC), "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "Notes"}),
            Some("Notes"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");
        let [footnote]: [Footnote; 1] = result.footnotes.try_into().expect("one footnote");
        let footnote: &Footnote = &footnote;

        assert!(footnote.provisional);
        assert!(!footnote.text.contains("(20"), "{}", footnote.text);
    }

    #[test]
    fn test_title_falls_back_to_the_document_title() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entry_text("c1", DOC), "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "year": "2020"}),
            Some("Dabaris"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");
        let [footnote]: [Footnote; 1] = result.footnotes.try_into().expect("one footnote");
        let footnote: &Footnote = &footnote;

        assert!(footnote.text.contains("*Dabaris*"), "{}", footnote.text);
        assert!(!footnote.provisional);
    }

    #[test]
    fn test_unknown_document_is_provisional() {
        let dir = tempfile::tempdir().expect("tempdir");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entry_text("c1", DOC), "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");

        let result =
            block_on(renderer(HashMap::new(), dir.path()).render("essay.md")).expect("renders");
        let [footnote]: [Footnote; 1] = result.footnotes.try_into().expect("one footnote");
        let footnote: &Footnote = &footnote;

        assert!(footnote.provisional);
        assert!(
            footnote.text.contains(&format!("document {DOC}")),
            "{}",
            footnote.text
        );
    }

    #[test]
    fn test_footnotes_emit_in_id_order() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        let entries = entry_text("c2", DOC) + &entry_text("c1", DOC);
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entries, "First [^c2], then [^c1].\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "N", "year": "2020"}),
            Some("N"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");

        let ids = result
            .footnotes
            .iter()
            .map(|note| note.id.clone())
            .collect::<Vec<_>>();
        assert_eq!(ids, vec!["c1".to_owned(), "c2".to_owned()]);
    }

    #[test]
    fn test_numeric_sort_puts_c10_after_c2() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        let entries = entry_text("c10", DOC) + &entry_text("c2", DOC);
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entries, "Tenth [^c10], second [^c2].\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "N", "year": "2020"}),
            Some("N"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");

        let ids = result
            .footnotes
            .iter()
            .map(|note| note.id.clone())
            .collect::<Vec<_>>();
        assert_eq!(ids, vec!["c2".to_owned(), "c10".to_owned()]);
    }

    #[test]
    fn test_stale_definitions_are_replaced_not_doubled() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(
                &entry_text("c1", DOC),
                "Reading [^c1] closely.\n\n[^c1]: a stale hand-typed note.\n",
            ),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "N", "year": "2020"}),
            Some("N"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");

        assert!(!result.rendered.contains("stale"), "{}", result.rendered);
        assert_eq!(
            result.rendered.matches("[^c1]:").count(),
            1,
            "{}",
            result.rendered
        );
    }

    #[test]
    fn test_edition_and_date_key_render() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        let mut entries = entry_text("c1", DOC);
        entries.push_str("    edition: Dabaris Reader\n");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entries, "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        // `date` backs `year` when `year` is absent.
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "N", "date": "2021-05-06"}),
            Some("N"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");
        let [footnote]: [Footnote; 1] = result.footnotes.try_into().expect("one footnote");
        let footnote: &Footnote = &footnote;

        assert!(
            footnote
                .text
                .starts_with("Anon, *N*, Dabaris Reader, vol. II (2021), p. 64. [normalized]"),
            "{}",
            footnote.text
        );
        assert!(!footnote.provisional);
    }

    #[test]
    fn test_block_on_drives_a_pending_future_to_ready() {
        struct PendOnce(bool);
        impl Future for PendOnce {
            type Output = u32;
            fn poll(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<u32> {
                if self.0 {
                    Poll::Ready(7)
                } else {
                    self.0 = true;
                    context.waker().wake_by_ref();
                    Poll::Pending
                }
            }
        }
        assert_eq!(block_on(PendOnce(false)), 7);
    }

    #[test]
    fn test_block_on_survives_a_waker_clone() {
        struct CloneWaker;
        impl Future for CloneWaker {
            type Output = ();
            fn poll(self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<()> {
                // The no-op waker supports clone: dropping the clone here
                // exercises the raw-waker clone path.
                let _ = context.waker().clone();
                Poll::Ready(())
            }
        }
        block_on(CloneWaker);
    }

    #[test]
    fn test_footnote_year_spells_every_json_shape() {
        // Null is Python's missing `None`; bools spell as Python does.
        assert_eq!(footnote_year(Some(&json!(null))), None);
        assert_eq!(footnote_year(Some(&json!(true))), Some("True".to_owned()));
        assert_eq!(footnote_year(Some(&json!(false))), Some("False".to_owned()));
        // Collections serialize, then pass through untouched (no year prefix).
        assert_eq!(
            footnote_year(Some(&json!([2020]))),
            Some("[2020]".to_owned())
        );
        assert_eq!(
            footnote_year(Some(&json!({"year": 2020}))),
            Some("{\"year\":2020}".to_owned())
        );
    }

    fn footnote_entry() -> CitationEntry {
        CitationEntry {
            id: "c1".to_owned(),
            intent: marginalia_types::works_files::Intent::Support,
            role: None,
            document_id: Uuid::parse_str(DOC).expect("doc id"),
            char_start: 10,
            char_end: 60,
            quoted_text: "words".to_owned(),
            edition: None,
            edition_key: None,
            locator: serde_json::Map::new(),
        }
    }

    #[test]
    fn test_locator_scalars_display_like_python() {
        let id = Uuid::parse_str(DOC).expect("doc id");
        let (_, document) = metadata_doc(
            json!({"author": "Anon", "title": "N", "year": "2020"}),
            Some("N"),
            id,
        );
        // Null, bools, and collections in locators spell as Python would.
        let mut entry = footnote_entry();
        entry.locator.insert("page".to_owned(), Value::Null);
        let note = build_footnote(&entry, Some(&document), "exact");
        assert!(note.text.contains("p. None"), "{}", note.text);
        let mut entry = footnote_entry();
        entry.locator.insert("flag".to_owned(), Value::Bool(true));
        let note = build_footnote(&entry, Some(&document), "exact");
        assert!(note.text.contains("flag True"), "{}", note.text);
        let mut entry = footnote_entry();
        entry.locator.insert("flag".to_owned(), Value::Bool(false));
        let note = build_footnote(&entry, Some(&document), "exact");
        assert!(note.text.contains("flag False"), "{}", note.text);
        let mut entry = footnote_entry();
        entry.locator.insert("pages".to_owned(), json!([1, 2]));
        let note = build_footnote(&entry, Some(&document), "exact");
        // Collections serialize as compact JSON, never Python `repr`.
        assert!(note.text.contains("pages [1,2]"), "{}", note.text);
    }

    #[test]
    fn test_entry_without_citations_renders_the_body_untouched() {
        let dir = tempfile::tempdir().expect("tempdir");
        // No `citations:` key at all: an empty value would be YAML null,
        // which is a hard error, not an empty list.
        std::fs::write(
            dir.path().join("essay.md"),
            "---\n\
             work: W-001\n\
             title: \"A fragment\"\n\
             type: essay\n\
             status: draft\n\
             created: 2026-09-04\n\
             claims: []\n\
             ---\n\nJust prose, no markers.\n",
        )
        .expect("fixture write");
        let id = Uuid::parse_str(DOC).expect("doc id");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "N", "year": "2020"}),
            Some("N"),
            id,
        )]);

        let result = block_on(renderer(docs, dir.path()).render("essay.md")).expect("renders");

        assert!(result.footnotes.is_empty());
        let body = WorkFileReader::read(&WorkFileReader::new(dir.path().to_path_buf()), "essay.md")
            .expect("fixture reads")
            .body;
        assert_eq!(result.rendered, body);
    }

    #[test]
    fn test_render_missing_file_is_an_error() {
        let dir = tempfile::tempdir().expect("tempdir");
        let error = block_on(renderer(HashMap::new(), dir.path()).render("gone.md"))
            .expect_err("missing file fails");
        assert!(error.to_string().contains("gone.md"), "{error}");
    }

    #[test]
    fn test_render_propagates_a_document_failure() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entry_text("c1", DOC), "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "N", "year": "2020"}),
            Some("N"),
            id,
        )]);
        // A failing document read fails the footnote and the render.
        let error = block_on(renderer_with(docs, dir.path(), true, false).render("essay.md"))
            .expect_err("document failure fails");
        assert_eq!(error.to_string(), "database or storage error: docs broke");
    }

    #[test]
    fn test_render_propagates_a_verification_failure() {
        let dir = tempfile::tempdir().expect("tempdir");
        let id = Uuid::parse_str(DOC).expect("doc id");
        std::fs::write(
            dir.path().join("essay.md"),
            file_text(&entry_text("c1", DOC), "Reading [^c1] closely.\n"),
        )
        .expect("fixture write");
        let docs = HashMap::from([metadata_doc(
            json!({"author": "Anon", "title": "N", "year": "2020"}),
            Some("N"),
            id,
        )]);
        // The document read succeeds, so the failure is the verify call.
        let error = block_on(renderer_with(docs, dir.path(), false, true).render("essay.md"))
            .expect_err("verification failure fails");
        assert_eq!(error.to_string(), "database or storage error: verify broke");
    }

    #[test]
    fn test_fake_documents_round_trip_every_method() {
        use marginalia_types::documents::{DocumentDraft, DocumentFilter};
        let repo = FakeDocuments {
            docs: Mutex::new(HashMap::new()),
            fail: false,
        };
        assert_eq!(block_on(repo.count(None)).expect("count"), 0);
        let draft = DocumentDraft {
            title: Some("Dabaris".to_owned()),
            document_type: "generic".to_owned(),
            language: None,
            source: "test".to_owned(),
            content_hash: vec![7u8; 32],
            parser: "test".to_owned(),
            parser_version: "1".to_owned(),
            created_date_start: None,
            created_date_end: None,
            created_precision: None,
            edition_id: None,
            metadata: serde_json::Map::new(),
        };
        let inserted = block_on(repo.insert(&mut (), draft)).expect("insert stores");
        assert_eq!(block_on(repo.count(None)).expect("count"), 1);
        assert_eq!(
            block_on(repo.get(inserted.id))
                .expect("get")
                .expect("present")
                .title,
            Some("Dabaris".to_owned())
        );
        assert!(block_on(repo.get(Uuid::new_v4())).expect("get").is_none());
        let many = block_on(repo.get_many(&[inserted.id, Uuid::new_v4()])).expect("get_many");
        assert_eq!(
            many.iter().map(|doc| doc.id).collect::<Vec<_>>(),
            vec![inserted.id]
        );
        assert_eq!(
            block_on(repo.find_by_hash(&[7u8; 32], "test"))
                .expect("find")
                .map(|doc| doc.id),
            Some(inserted.id)
        );
        assert!(block_on(repo.find_by_hash(&[8u8; 32], "test"))
            .expect("find")
            .is_none());
        let mut patch = serde_json::Map::new();
        patch.insert("author".to_owned(), json!("Kittel"));
        let updated = block_on(repo.update_metadata(inserted.id, patch)).expect("patch merges");
        assert_eq!(updated.metadata.get("author"), Some(&json!("Kittel")));
        let missing = Uuid::new_v4();
        assert!(
            block_on(repo.update_metadata(missing, serde_json::Map::new())).is_err(),
            "patching a missing row fails"
        );
        assert_eq!(
            block_on(repo.iter_by_filter(&DocumentFilter::default()))
                .expect("iter")
                .len(),
            1
        );
        block_on(repo.delete(inserted.id)).expect("delete removes");
        assert_eq!(block_on(repo.count(None)).expect("count"), 0);
        assert!(block_on(repo.get(inserted.id)).expect("get").is_none());
    }
}
