//! Correspondent-name reduction and corpus-holdings indexing.
//!
//! Ports `history/tools/_holdings.py`: [`surname`], [`direction_of`], and
//! [`Holdings`] with its [`Holdings::coverage_note`] caveat. The async
//! [`build`](https://example.invalid) stays Python: it reads dated sections
//! through the plugin SDK's corpus client, which is framework, not math.

use std::collections::HashMap;

use marginalia_text::chars::{is_space, strip};
use marginalia_text::word_table::is_word_char;

/// Which way a reference points, from the kind of reference it is.
pub const SENT: &str = "sent_by_author";
/// A letter the author received.
pub const RECEIVED: &str = "received_by_author";
/// A mention whose direction the kind does not settle.
pub const UNKNOWN: &str = "unknown";

/// Ranks, honorifics and offices that precede a name without being part of it.
const NOISE: &[&str] = &[
    "genl",
    "gen",
    "general",
    "maj",
    "major",
    "lt",
    "lieut",
    "lieutenant",
    "col",
    "colonel",
    "capt",
    "captain",
    "brig",
    "brigadier",
    "hon",
    "honorable",
    "mr",
    "mrs",
    "dr",
    "his",
    "her",
    "excellency",
    "the",
    "secty",
    "secretary",
    "of",
    "war",
    "president",
    "esq",
    "comdg",
    "commanding",
    "us",
    "army",
    "sir",
    "my",
    "dear",
    "state",
    "treasury",
    "navy",
    "govr",
    "gov",
    "governor",
    "your",
    "presdt",
    "prest",
    "adj",
    "adjt",
    "adjutant",
    "to",
    "from",
    "asst",
    "assistant",
    "chief",
    "staff",
    "private",
    "confidential",
];

const PRONOUNS: &[&str] = &[
    "you", "him", "her", "them", "me", "us", "he", "she", "it", "they",
];

/// The one part of a name every variant of it agrees on.
///
/// "Stanton", "Edwin M. Stanton" and "Hon E M Stanton Secty of War" are one
/// man; rank and office differ, the surname does not.
pub fn surname(name: Option<&str>) -> Option<String> {
    let name = name.filter(|n| !n.is_empty())?;
    let cleaned: String = strip_gbm_prefix(name)
        .chars()
        .map(|c| {
            if is_word_char(c) || is_space(c) || c == '.' {
                c
            } else {
                ' '
            }
        })
        .collect();
    let cleaned = strip(&cleaned);
    let mut words: Vec<&str> = Vec::new();
    for word in cleaned.split(is_space).filter(|w| !w.is_empty()) {
        // `strip(".")` trims ASCII dots only; `to_lowercase` is the Unicode
        // full mapping both sides use.
        if !NOISE.contains(&word.trim_matches('.').to_lowercase().as_str()) {
            words.push(word);
        }
    }
    // Bare initials are not a name (`w.strip()` is a no-op post-split, so the
    // test runs on the word itself).
    words.retain(|w| !is_initial(w));
    let last = words.last()?;
    let last = last.trim_matches('.').to_lowercase();
    // `last.isdigit()` in Python is Nd + No; `is_numeric` adds Nl (Roman
    // numerals like "VIII"). A multi-letter numeral reaching this gate as a
    // surname is absurd — accepted residual divergence, same class as the
    // Phase 4 non-ASCII hex finding — pinned here only for realistic inputs.
    // Python lowercases with context-sensitive sigma (final Σ → ς); Rust maps
    // Σ → σ everywhere. Greek surnames ending in sigma diverge — same class.
    if PRONOUNS.contains(&last.as_str())
        || last.chars().count() < 3
        || last.chars().all(|c| c.is_numeric())
    {
        return None;
    }
    Some(last)
}

/// `re.sub(r"^\s*GBM\s+to\s+", "", name, flags=re.I)`: the one anchored match.
///
/// Case-insensitivity is exact here: the pattern letters (G, B, M, T, O)
/// have no non-ASCII case variants, and `to_ascii_lowercase` maps A–Z only.
fn strip_gbm_prefix(name: &str) -> &str {
    let rest = name.trim_start_matches(is_space);
    let bytes = rest.as_bytes();
    if bytes.len() >= 3
        && bytes[0].eq_ignore_ascii_case(&b'g')
        && bytes[1].eq_ignore_ascii_case(&b'b')
        && bytes[2].eq_ignore_ascii_case(&b'm')
    {
        // Byte 3 is safe: the match above proved the first three are ASCII.
        let after = rest[3..].trim_start_matches(is_space);
        if after.len() != rest[3..].len() {
            let tail = after.as_bytes();
            if tail.len() >= 2
                && tail[0].eq_ignore_ascii_case(&b't')
                && tail[1].eq_ignore_ascii_case(&b'o')
            {
                let after_to = after[2..].trim_start_matches(is_space);
                if after_to.len() != after[2..].len() {
                    return after_to;
                }
            }
        }
    }
    name
}

/// `re.fullmatch(r"[A-Z]\.?", word)`: one ASCII uppercase letter, optional dot.
fn is_initial(word: &str) -> bool {
    let bytes = word.as_bytes();
    (bytes.len() == 1 && bytes[0].is_ascii_uppercase())
        || (bytes.len() == 2 && bytes[0].is_ascii_uppercase() && bytes[1] == b'.')
}

/// Which way a reference points, from the kind of reference it is.
pub fn direction_of(reference_type: Option<&str>) -> &'static str {
    match reference_type.unwrap_or("") {
        "prior_letter" | "enclosure" => SENT,
        "received_letter" => RECEIVED,
        _ => UNKNOWN,
    }
}

/// The letters a corpus contains, indexed by correspondent and date.
pub struct Holdings {
    by_key: HashMap<(String, String), String>,
    /// Titles starting with "To " (case-insensitive): the author's outgoing.
    pub outgoing: usize,
    /// Titles starting with "From ": the author's incoming.
    pub incoming: usize,
    /// Titles stating neither direction.
    pub undirected: usize,
}

impl Holdings {
    /// Empty holdings, mirroring `Holdings()`.
    pub fn new() -> Self {
        Self {
            by_key: HashMap::new(),
            outgoing: 0,
            incoming: 0,
            undirected: 0,
        }
    }

    /// Index one dated section title. Undated or empty inputs are skipped —
    /// an undated section cannot be looked up, so it is not indexed.
    pub fn add(&mut self, title: Option<&str>, date_start: Option<&str>) {
        let (Some(title), Some(date_start)) = (title, date_start) else {
            return;
        };
        if title.is_empty() || date_start.is_empty() {
            return;
        }
        let stripped = strip(title);
        let lowered = stripped.to_lowercase();
        if lowered.starts_with("to ") {
            self.outgoing += 1;
        } else if lowered.starts_with("from ") {
            self.incoming += 1;
        } else {
            self.undirected += 1;
        }
        if let Some(who) = surname(Some(stripped)) {
            // Python `date_start[:10]` slices code points, not bytes.
            let day: String = date_start.chars().take(10).collect();
            self.by_key.insert((who, day), stripped.to_owned());
        }
    }

    /// The title of the letter matching this correspondent and day, if any.
    /// Matching is by day, not by timestamp — and a missing date means no
    /// lookup, not a miss.
    pub fn held(&self, who: Option<&str>, date_start: Option<&str>) -> Option<&str> {
        let (Some(who), Some(date_start)) = (who, date_start) else {
            return None;
        };
        if who.is_empty() || date_start.is_empty() {
            return None;
        }
        let day: String = date_start.chars().take(10).collect();
        self.by_key.get(&(who.to_owned(), day)).map(String::as_str)
    }

    /// Dated letters indexed.
    pub fn total(&self) -> usize {
        self.by_key.len()
    }

    /// Say once what the corpus cannot contain, rather than per reference.
    pub fn coverage_note(&self) -> Option<String> {
        if self.outgoing > 0 && self.incoming == 0 {
            return Some(format!(
                "This corpus holds {} letters written by the author and none received by him, \
                 so an inbound reference is necessarily absent from it — that is a property \
                 of the edition, not a discovery about the archive.",
                self.outgoing
            ));
        }
        if self.incoming > 0 && self.outgoing == 0 {
            return Some(format!(
                "This corpus holds {} letters received by the author and none sent by him, \
                 so an outbound reference is necessarily absent from it.",
                self.incoming
            ));
        }
        None
    }
}
impl Default for Holdings {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_empty_holdings() {
        let holdings = Holdings::default();
        assert_eq!(holdings.total(), 0);
        assert_eq!(holdings.coverage_note(), None);
    }

    // Mirrors packages/plugins/history/tests/test_holdings.py case-for-case.

    #[test]
    fn one_man_written_four_ways() {
        for written in [
            "Stanton",
            "Edwin M. Stanton",
            "Hon E M Stanton Secty of War",
            "Hon. Edwin M. Stanton, Secretary of War",
        ] {
            assert_eq!(
                surname(Some(written)),
                Some("stanton".to_owned()),
                "{written}"
            );
        }
    }

    #[test]
    fn editors_reference_line_is_not_a_name() {
        assert_eq!(
            surname(Some("GBM to Andrew Porter")),
            Some("porter".to_owned())
        );
    }

    #[test]
    fn gbm_prefix_needs_both_words() {
        // "GBM" alone is a surname candidate, not a reference line.
        assert_eq!(surname(Some("GBM")), Some("gbm".to_owned()));
        assert_eq!(surname(Some("gbm to")), Some("gbm".to_owned()));
        // Whitespace without the second word is no reference line either.
        assert_eq!(surname(Some("GBM xyz")), Some("xyz".to_owned()));
    }

    #[test]
    fn things_that_are_not_a_person() {
        for written in ["you", "him", "the President", "Genl", ""] {
            assert_eq!(surname(Some(written)), None, "{written}");
        }
        assert_eq!(surname(None), None);
    }

    #[test]
    fn pronouns_and_digits_rejected() {
        assert_eq!(surname(Some("To Him")), None);
        assert_eq!(surname(Some("Letter 1862")), None);
    }

    #[test]
    fn the_authors_own_letters() {
        assert_eq!(direction_of(Some("prior_letter")), SENT);
        assert_eq!(direction_of(Some("enclosure")), SENT);
    }

    #[test]
    fn letters_to_the_author() {
        assert_eq!(direction_of(Some("received_letter")), RECEIVED);
    }

    #[test]
    fn everything_else_is_unknown() {
        assert_eq!(direction_of(Some("mentioned_letter")), UNKNOWN);
        assert_eq!(direction_of(Some("third_party_letter")), UNKNOWN);
        assert_eq!(direction_of(None), UNKNOWN);
        assert_eq!(direction_of(Some("")), UNKNOWN);
    }

    fn corpus() -> Holdings {
        let mut holdings = Holdings::new();
        holdings.add(
            Some("To Henry W. Halleck"),
            Some("1862-10-25T00:00:00+00:00"),
        );
        holdings.add(Some("To Winfield Scott"), Some("1861-04-27T00:00:00+00:00"));
        holdings
    }

    #[test]
    fn a_letter_we_hold_is_found() {
        assert_eq!(
            corpus().held(Some("halleck"), Some("1862-10-25")),
            Some("To Henry W. Halleck")
        );
    }

    #[test]
    fn matching_is_by_day_not_by_timestamp() {
        assert_eq!(
            corpus().held(Some("halleck"), Some("1862-10-25T14:30:00+00:00")),
            Some("To Henry W. Halleck")
        );
    }

    #[test]
    fn right_man_wrong_day_is_not_a_match() {
        assert_eq!(corpus().held(Some("halleck"), Some("1862-10-26")), None);
    }

    #[test]
    fn wrong_man_right_day_is_not_a_match() {
        assert_eq!(corpus().held(Some("stanton"), Some("1862-10-25")), None);
    }

    #[test]
    fn no_date_means_no_lookup() {
        assert_eq!(corpus().held(Some("halleck"), None), None);
        assert_eq!(corpus().held(None, Some("1862-10-25")), None);
        assert_eq!(corpus().held(Some(""), Some("1862-10-25")), None);
        assert_eq!(corpus().held(Some("halleck"), Some("")), None);
    }

    #[test]
    fn undated_or_empty_sections_are_not_indexed() {
        let mut holdings = Holdings::new();
        holdings.add(Some("To Someone"), None);
        holdings.add(None, Some("1862-10-25T00:00:00+00:00"));
        holdings.add(Some(""), Some("1862-10-25T00:00:00+00:00"));
        holdings.add(Some("To Someone"), Some(""));
        assert_eq!(holdings.total(), 0);
        assert_eq!(holdings.outgoing, 0);
        // A blank title still states no direction, so it counts as undirected.
        holdings.add(Some("   "), Some("1862-10-25T00:00:00+00:00"));
        assert_eq!(holdings.total(), 0);
        assert_eq!(holdings.undirected, 1);
    }

    #[test]
    fn outgoing_edition_says_so_once() {
        let mut holdings = Holdings::new();
        for day in 1..4 {
            holdings.add(
                Some("To Halleck"),
                Some(format!("1862-10-0{day}T00:00:00+00:00").as_str()),
            );
        }
        let note = holdings
            .coverage_note()
            .expect("outgoing-only corpus warns");
        assert!(note.contains("none received by him"), "{note}");
        assert!(note.contains("3 letters written by the author"), "{note}");
    }

    #[test]
    fn incoming_edition_says_the_converse() {
        let mut holdings = Holdings::new();
        holdings.add(Some("From Halleck"), Some("1862-10-01T00:00:00+00:00"));
        let note = holdings
            .coverage_note()
            .expect("incoming-only corpus warns");
        assert!(note.contains("none sent by him"), "{note}");
    }

    #[test]
    fn corpus_holding_both_directions_needs_no_caveat() {
        let mut holdings = Holdings::new();
        holdings.add(Some("To Halleck"), Some("1862-10-01T00:00:00+00:00"));
        holdings.add(Some("From Halleck"), Some("1862-10-02T00:00:00+00:00"));
        assert_eq!(holdings.coverage_note(), None);
    }

    #[test]
    fn titles_stating_no_direction() {
        let mut holdings = Holdings::new();
        holdings.add(
            Some("Memorandum for the President"),
            Some("1862-10-01T00:00:00+00:00"),
        );
        assert_eq!(holdings.undirected, 1);
        assert_eq!(holdings.coverage_note(), None);
    }
}
