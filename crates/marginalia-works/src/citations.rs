//! Which works cite this source — a grep with a stable shape.
//!
//! Python source: `services/works/citations.py` (`WorkCitationFinder`).
//! Everything reports `"source": "files"`; the mirror switch lives in the
//! tool, keyed off a container flag that does not exist here.

use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use marginalia_types::works_files::{Intent, WorkFileStatus};

use crate::files::WorkFileReader;

/// One entry (or claim ref) matching the selector, with the same nullable
/// shape as the Python match dicts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationMatch {
    pub work_path: String,
    pub work: String,
    pub title: String,
    pub status: String,
    #[serde(default)]
    pub citation_id: Option<String>,
    #[serde(default)]
    pub intent: Option<String>,
    #[serde(default)]
    pub document_id: Option<String>,
    #[serde(default)]
    pub char_start: Option<i64>,
    #[serde(default)]
    pub char_end: Option<i64>,
    #[serde(default)]
    pub quoted_text: Option<String>,
    /// Absent on entry matches, like the Python dicts (claim matches carry
    /// explicit nulls for the entry keys but always name their `claim_ref`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claim_ref: Option<String>,
}

/// Finder output: matches plus the `"files"` source tag.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationSearch {
    pub matches: Vec<CitationMatch>,
    pub source: String,
}

/// Entries (or claim refs) across every work file, filtered by one key.
pub struct WorkCitationFinder {
    reader: WorkFileReader,
}

impl WorkCitationFinder {
    pub fn new(works_dir: PathBuf) -> Self {
        Self {
            reader: WorkFileReader::new(works_dir),
        }
    }

    /// Scan every work file; one bad file hides no citation (skipped, like
    /// the Python `logger.warning` + `continue` — no logger is wired here).
    pub async fn find(
        &self,
        document_id: Option<Uuid>,
        edition_key: Option<&str>,
        claim_ref: Option<&str>,
    ) -> CitationSearch {
        let mut matches = Vec::new();
        for work_path in self.reader.list_works() {
            let Ok(work) = self.reader.read(&work_path) else {
                continue;
            };
            let front = &work.front_matter;
            // Claim branch: one row per work whose `claims:` names the ref;
            // entries are not scanned, so a claim query never returns spans.
            if let Some(claim) = claim_ref {
                if front.claims.iter().any(|name| name == claim) {
                    matches.push(CitationMatch {
                        work_path: work.work_path.clone(),
                        work: front.work.clone(),
                        title: front.title.clone(),
                        status: status_value(front.status),
                        citation_id: None,
                        intent: None,
                        document_id: None,
                        char_start: None,
                        char_end: None,
                        quoted_text: None,
                        claim_ref: Some(claim.to_owned()),
                    });
                }
                continue;
            }
            for entry in &front.citations {
                if document_id.is_some_and(|id| entry.document_id != id) {
                    continue;
                }
                if edition_key.is_some_and(|key| entry.edition_key.as_deref() != Some(key)) {
                    continue;
                }
                matches.push(CitationMatch {
                    work_path: work.work_path.clone(),
                    work: front.work.clone(),
                    title: front.title.clone(),
                    status: status_value(front.status),
                    citation_id: Some(entry.id.clone()),
                    intent: Some(intent_value(entry.intent)),
                    document_id: Some(entry.document_id.to_string()),
                    char_start: Some(entry.char_start),
                    char_end: Some(entry.char_end),
                    quoted_text: Some(entry.quoted_text.clone()),
                    claim_ref: None,
                });
            }
        }
        CitationSearch {
            matches,
            source: "files".to_owned(),
        }
    }
}

/// The `status.value` string Python interpolates into each match.
fn status_value(status: WorkFileStatus) -> String {
    match status {
        WorkFileStatus::Draft => "draft",
        WorkFileStatus::Review => "review",
        WorkFileStatus::Published => "published",
    }
    .to_owned()
}

/// The `intent.value` string Python interpolates into each entry match.
fn intent_value(intent: Intent) -> String {
    match intent {
        Intent::Quotation => "quotation",
        Intent::Translation => "translation",
        Intent::Support => "support",
        Intent::Contrast => "contrast",
        Intent::Background => "background",
        Intent::Definition => "definition",
        Intent::Source => "source",
        Intent::SeeAlso => "see_also",
    }
    .to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future::Future;
    use std::pin::Pin;
    use std::task::{Context, Poll, RawWaker, RawWakerVTable, Waker};

    /// Minimal executor: `find` never pends on local files, so a spin poll
    /// drives it to readiness. (`tokio` is not a dependency of this crate,
    /// and `Cargo.toml` is owned by another slice.)
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

    /// Mirrors the `fixture_work.md` shape behind
    /// `tests/integration/test_works.py::test_citations_find_the_fixture_work`:
    /// seven entries on one document/edition plus a `TEST-001` claim.
    fn fixture_text(doc: &str) -> String {
        let mut text = "---\n\
             work: W-001\n\
             title: \"A dabaris fragment\"\n\
             type: essay\n\
             status: draft\n\
             created: 2026-09-04\n\
             claims: [TEST-001]\n\
             citations:\n"
            .to_owned();
        let quotes = [
            "The prophets pair two words.",
            "He requires justice of every ruler",
            "Dabar is word, matter, thing",
            "The prophets proclaim righteousness",
            "He requires justice of every king",
            "A marginal gloss never entered here",
            "of every ruler, a phrase uneven",
        ];
        for (index, quote) in quotes.iter().enumerate() {
            let id = index + 1;
            text.push_str(&format!(
                "  - id: c{id}\n\
                 \x20   document_id: {doc}\n\
                 \x20   char_start: {}\n\
                 \x20   char_end: {}\n\
                 \x20   quoted_text: \"{quote}\"\n\
                 \x20   intent: quotation\n\
                 \x20   edition_key: DABAR_2026\n",
                id * 10,
                id * 10 + 20,
            ));
        }
        text.push_str("---\n\n## Notes\n\nReading [^c1] closely.\n");
        text
    }

    fn write(dir: &std::path::Path, name: &str, text: &str) {
        std::fs::write(dir.join(name), text).expect("fixture write");
    }

    #[test]
    fn test_find_by_document_returns_every_entry() {
        let dir = tempfile::tempdir().expect("tempdir");
        let doc = Uuid::new_v4();
        write(dir.path(), "essay.md", &fixture_text(&doc.to_string()));
        let finder = WorkCitationFinder::new(dir.path().to_path_buf());

        let found = block_on(finder.find(Some(doc), None, None));

        let ids = found
            .matches
            .iter()
            .map(|item| item.citation_id.clone().expect("entry match has id"))
            .collect::<Vec<_>>();
        assert_eq!(
            ids,
            vec![
                "c1".to_owned(),
                "c2".to_owned(),
                "c3".to_owned(),
                "c4".to_owned(),
                "c5".to_owned(),
                "c6".to_owned(),
                "c7".to_owned(),
            ]
        );
        assert!(found
            .matches
            .iter()
            .all(|item| item.work_path == "essay.md"));
        assert_eq!(found.source, "files");
        let first = &found.matches[0];
        assert_eq!(first.work, "W-001");
        assert_eq!(first.title, "A dabaris fragment");
        assert_eq!(first.status, "draft");
        assert_eq!(first.intent.as_deref(), Some("quotation"));
        assert_eq!(
            first.document_id.as_deref(),
            Some(doc.to_string()).as_deref()
        );
        assert_eq!((first.char_start, first.char_end), (Some(10), Some(30)));
        assert_eq!(
            first.quoted_text.as_deref(),
            Some("The prophets pair two words.")
        );
        assert_eq!(first.claim_ref, None);
    }

    #[test]
    fn test_find_by_edition_filters_and_missing_key_is_empty() {
        let dir = tempfile::tempdir().expect("tempdir");
        let doc = Uuid::new_v4();
        write(dir.path(), "essay.md", &fixture_text(&doc.to_string()));
        let finder = WorkCitationFinder::new(dir.path().to_path_buf());

        let found = block_on(finder.find(None, Some("DABAR_2026"), None));
        assert_eq!(found.matches.len(), 7);

        let missing = block_on(finder.find(None, Some("NO_SUCH_KEY"), None));
        assert!(missing.matches.is_empty());
    }

    #[test]
    fn test_find_by_claim_returns_the_work_not_spans() {
        let dir = tempfile::tempdir().expect("tempdir");
        let doc = Uuid::new_v4();
        write(dir.path(), "essay.md", &fixture_text(&doc.to_string()));
        let finder = WorkCitationFinder::new(dir.path().to_path_buf());

        let found = block_on(finder.find(None, None, Some("TEST-001")));

        assert_eq!(found.matches.len(), 1);
        let only = &found.matches[0];
        assert_eq!(only.claim_ref.as_deref(), Some("TEST-001"));
        assert_eq!(only.citation_id, None);
        assert_eq!(only.intent, None);
        assert_eq!(only.document_id, None);
        assert_eq!(only.char_start, None);
        assert_eq!(only.char_end, None);
        assert_eq!(only.quoted_text, None);

        let missing = block_on(finder.find(None, None, Some("NO-SUCH-CLAIM")));
        assert!(missing.matches.is_empty());
    }

    #[test]
    fn test_unreadable_file_hides_no_citation() {
        let dir = tempfile::tempdir().expect("tempdir");
        let doc = Uuid::new_v4();
        write(dir.path(), "essay.md", &fixture_text(&doc.to_string()));
        write(dir.path(), "broken.md", "no fence at all\n");
        let finder = WorkCitationFinder::new(dir.path().to_path_buf());

        let found = block_on(finder.find(Some(doc), None, None));

        assert_eq!(found.matches.len(), 7);
    }

    #[test]
    fn test_wrong_document_matches_nothing() {
        let dir = tempfile::tempdir().expect("tempdir");
        write(
            dir.path(),
            "essay.md",
            &fixture_text(&Uuid::new_v4().to_string()),
        );
        let finder = WorkCitationFinder::new(dir.path().to_path_buf());

        let found = block_on(finder.find(Some(Uuid::new_v4()), None, None));

        assert!(found.matches.is_empty());
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
    fn test_review_and_published_statuses_report_their_values() {
        let dir = tempfile::tempdir().expect("tempdir");
        let doc = Uuid::new_v4();
        write(
            dir.path(),
            "review.md",
            &fixture_text(&doc.to_string()).replace("status: draft", "status: review"),
        );
        write(
            dir.path(),
            "published.md",
            &fixture_text(&doc.to_string()).replace("status: draft", "status: published"),
        );
        let finder = WorkCitationFinder::new(dir.path().to_path_buf());

        let found = block_on(finder.find(Some(doc), None, None));

        let mut statuses = found
            .matches
            .iter()
            .map(|item| (item.work_path.clone(), item.status.clone()))
            .collect::<Vec<_>>();
        statuses.sort();
        statuses.dedup();
        assert_eq!(
            statuses,
            vec![
                ("published.md".to_owned(), "published".to_owned()),
                ("review.md".to_owned(), "review".to_owned()),
            ]
        );
    }

    #[test]
    fn test_every_intent_reports_its_value() {
        let dir = tempfile::tempdir().expect("tempdir");
        let doc = Uuid::new_v4();
        let intents = [
            "quotation",
            "translation",
            "support",
            "contrast",
            "background",
            "definition",
            "source",
            "see_also",
        ];
        let mut entries = String::new();
        for (index, intent) in intents.iter().enumerate() {
            let id = index + 1;
            entries.push_str(&format!(
                "  - id: c{id}\n\
                 \x20   document_id: {doc}\n\
                 \x20   char_start: {}\n\
                 \x20   char_end: {}\n\
                 \x20   quoted_text: \"words number {id}\"\n\
                 \x20   intent: {intent}\n",
                id * 10,
                id * 10 + 20,
            ));
        }
        let text = format!(
            "---\n\
             work: W-001\n\
             title: \"Intents\"\n\
             type: essay\n\
             status: draft\n\
             created: 2026-09-04\n\
             claims: []\n\
             citations:\n{entries}---\n\nBody.\n"
        );
        write(dir.path(), "essay.md", &text);
        let finder = WorkCitationFinder::new(dir.path().to_path_buf());

        let found = block_on(finder.find(Some(doc), None, None));

        let mut reported = found
            .matches
            .iter()
            .map(|item| item.intent.clone().expect("entry match has intent"))
            .collect::<Vec<_>>();
        reported.sort();
        let mut expected = intents
            .iter()
            .map(|intent| (*intent).to_owned())
            .collect::<Vec<_>>();
        expected.sort();
        assert_eq!(reported, expected);
    }
}
