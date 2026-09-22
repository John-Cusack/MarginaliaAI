//! Verse-identity lemma lookup over `core.words`.
//!
//! Python source: `services/words/lookup.py`; the table and column names the
//! SQL builders assume come from migrations `014_words`
//! (`core.words`), `015_words_language_strong` (the `(language, strong)`
//! index), and `016_versification` (`core.verse_map`, `core.edition_books`).
//!
//! The constraint that shapes this whole module: **it returns verse
//! references and never character spans.** `core.words` indexes the
//! Westminster Leningrad Codex while callers quote the Lexham Hebrew Bible,
//! and a character span into WLC does not address the same characters in
//! LHB. A verse reference survives the hop, because verse *identity* is what
//! the two editions share. There are therefore no `char_start`, `char_end`,
//! `document_id`, `position`, or `id` fields anywhere in this module.
//!
//! The second hop is versification. `core.words.ref` is in the Hebrew
//! scheme and the English tradition puts 1,978 of those verses somewhere
//! else, so each occurrence carries its English reference too, taken from
//! `core.verse_map` (see [`english_reference`]).
//!
//! No PyO3 seam, no DB execution here: this module exposes SQL-string
//! builders with `$N` placeholders plus ordered params, and pure decision
//! functions. The later `repos` pass executes them via `sqlx`.

use serde::{Deserialize, Serialize};

use crate::{Error, Result};

/// Refuse rather than truncate past this many occurrences. The two words the
/// index was built for return 422 and 157; a number an order of magnitude
/// larger is a request for a corpus dump, and the aggregates answer it
/// better.
pub const MAX_OCCURRENCES: usize = 2000;

/// The scheme pair every occurrence is reported against. `core.words.ref`
/// is written by the WLC ingest in the Masoretic numbering, and the
/// reference a caller quoting an English edition needs is the other side of
/// this pair.
pub const HEBREW: &str = "hebrew";
/// The English side of the [`HEBREW`] scheme pair.
pub const ENGLISH: &str = "english";

/// What to look up. `strong` alone is ambiguous in two different ways.
///
/// `language` disambiguates the lexicon — a Strong's number is unique only
/// inside one, and H4941 and G4941 are different words. It defaults rather
/// than being required because the column holds one language today, but it
/// is always part of the query, so the index that matters is
/// `(language, strong)`.
///
/// `homograph` disambiguates the *word*. OSHB splits entries Strong's
/// conflated and marks the halves with a letter: 59,061 rows carry one.
/// Leaving it out of the API would re-bake the exact conflation this index
/// exists to escape, so it is a first-class parameter with three states —
/// absent (every homograph), a letter (that one), or the empty string (only
/// rows with no letter). The empty string is modelled explicitly as
/// `Some("")`: it differs from absent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LemmaQuery {
    pub strong: String,
    pub language: String,
    pub homograph: Option<String>,
    pub book: Option<String>,
    pub chapter_start: Option<i32>,
    pub chapter_end: Option<i32>,
    pub include_occurrences: bool,
}

impl LemmaQuery {
    /// A lookup for `strong` in Hebrew with no narrowing.
    pub fn new(strong: impl Into<String>) -> Self {
        Self {
            strong: strong.into(),
            language: "he".to_string(),
            homograph: None,
            book: None,
            chapter_start: None,
            chapter_end: None,
            include_occurrences: true,
        }
    }

    /// The `query` echo carried by [`LemmaResult`]. `chapters` is the
    /// `[start, end]` pair when either bound is set, else null — mirroring
    /// the Python dict exactly.
    pub fn echo(&self) -> QueryEcho {
        QueryEcho {
            strong: self.strong.clone(),
            language: self.language.clone(),
            homograph: self.homograph.clone(),
            book: self.book.clone(),
            chapters: match (self.chapter_start, self.chapter_end) {
                (None, None) => None,
                (start, end) => Some([start, end]),
            },
        }
    }
}

/// The `query` map of a [`LemmaResult`]: what was looked up, echoed back.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueryEcho {
    pub strong: String,
    pub language: String,
    pub homograph: Option<String>,
    pub book: Option<String>,
    pub chapters: Option<[Option<i32>; 2]>,
}

/// Which of the outcomes [`english_reference`] reported for one occurrence.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mapping {
    /// There is no map to consult: the honest answer is unknown, not
    /// unchanged, so the English reference is withheld (null).
    Unmapped,
    /// The map is loaded and holds no row, so the traditions agree.
    Same,
    /// The map says where the verse moved.
    Full,
    /// The verse begins midway through another verse: reported with both
    /// halves rather than rounded.
    Partial,
}

impl Mapping {
    /// Decode `core.verse_map.mapping_type`, which is `NOT NULL` on a joined
    /// row and constrained to `'full'`/`'partial'`.
    pub fn from_mapping_type(s: &str) -> Result<Self> {
        match s {
            "full" => Ok(Self::Full),
            "partial" => Ok(Self::Partial),
            other => Err(Error::InvalidQuery(format!(
                "unknown verse_map mapping_type: {other:?}"
            ))),
        }
    }
}

/// One occurrence's English-side reference.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EnglishRef {
    /// The English-scheme reference, or null when [`Mapping::Unmapped`].
    pub r#ref: Option<String>,
    pub mapping: Mapping,
    /// The English half-verse (`to_part`), for partials.
    pub part: Option<String>,
    /// The Hebrew half-verse (`from_part`), for partials.
    pub hebrew_part: Option<String>,
}

/// Render one occurrence's English-side reference.
///
/// Split out and kept pure because this is where two very different facts
/// were once reported identically. A verse the traditions agree on has no
/// row in `core.verse_map`, and so did *every* verse when the map had not
/// been loaded — `mapping: "same"` in both cases. The second is not an
/// answer, and looked exactly like one: migration 016 creates the tables
/// but `scripts/load_versification.py` fills them, so a migrate without
/// that step left `find_lemma` confidently wrong about 1,978 verses.
///
/// Three outcomes now, and `unmapped` is not `same`:
///
/// * [`Mapping::Unmapped`] — there is no map to consult. `ref` is null,
///   because the honest answer is that this is unknown, not that it is
///   unchanged.
/// * [`Mapping::Same`] — the map is loaded and holds no row, so the
///   traditions agree.
/// * [`Mapping::Full`] / [`Mapping::Partial`] — the map says where the
///   verse moved.
pub fn english_reference(
    hebrew_ref: &str,
    to_ref: Option<&str>,
    to_part: Option<&str>,
    from_part: Option<&str>,
    mapping: Option<Mapping>,
    map_loaded: bool,
) -> EnglishRef {
    if !map_loaded {
        return EnglishRef {
            r#ref: None,
            mapping: Mapping::Unmapped,
            part: None,
            hebrew_part: None,
        };
    }
    match to_ref {
        None => EnglishRef {
            r#ref: Some(hebrew_ref.to_string()),
            mapping: Mapping::Same,
            part: None,
            hebrew_part: None,
        },
        Some(to) => EnglishRef {
            r#ref: Some(to.to_string()),
            // `mapping_type` is NOT NULL on a joined verse_map row, so a
            // mapped row always carries its outcome; a missing decode here
            // would mean the row failed to decode upstream, and is a
            // caller bug rather than a fourth outcome.
            mapping: mapping.expect("mapped verse_map row carries mapping_type"),
            part: to_part.map(str::to_string),
            hebrew_part: from_part.map(str::to_string),
        },
    }
}

/// One citable occurrence: verse identity plus morphology, never a span.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Occurrence {
    /// The Hebrew-scheme reference: what LHB and WLC both call this verse,
    /// and what the caller cites.
    pub r#ref: String,
    pub book: String,
    pub chapter: i32,
    pub verse: i32,
    pub english: EnglishRef,
    pub surface: String,
    pub lemma: String,
    pub morph: String,
    pub prefixes: Option<String>,
    pub homograph: Option<String>,
    pub from_qere: bool,
    /// Further halves of a partial mapping, folded onto the one occurrence
    /// (see [`fold_occurrences`]). Absent when empty, as in the Python dict.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub english_alternatives: Vec<EnglishRef>,
}

/// A decoded row of the occurrences `SELECT`, before English resolution and
/// partial folding. The `repos` pass decodes `sqlx` rows into this; the pure
/// fold below turns them into [`Occurrence`]s.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WordRow {
    pub r#ref: String,
    pub book: String,
    pub chapter: i32,
    pub verse: i32,
    pub surface: String,
    pub lemma: String,
    pub morph: String,
    pub prefixes: Option<String>,
    pub homograph: Option<String>,
    pub from_qere: bool,
    pub to_ref: Option<String>,
    pub to_part: Option<String>,
    pub from_part: Option<String>,
    pub mapping: Option<Mapping>,
}

/// Resolve each row's English reference and fold partial halves.
///
/// A partial maps twice, once per half (the join is on `from_ref` alone, so
/// the caller sees that the verse is split instead of silently receiving
/// whichever half sorted first). The halves fold onto the one occurrence
/// via `english_alternatives` when — and only when — the row matches the
/// previous occurrence's verse *and* surface and the previous mapping is
/// already partial. Non-partial duplicates are not folded: they are
/// distinct words that happen to share a surface.
/// One concrete owned type, deliberately not `impl IntoIterator`: a generic
/// signature monomorphizes per caller iterator type, and each copy needs
/// its own coverage for the same arms.
pub fn fold_occurrences(rows: Vec<WordRow>, map_loaded: bool) -> Vec<Occurrence> {
    let mut out: Vec<Occurrence> = Vec::new();
    for row in rows {
        let english = english_reference(
            &row.r#ref,
            row.to_ref.as_deref(),
            row.to_part.as_deref(),
            row.from_part.as_deref(),
            row.mapping,
            map_loaded,
        );
        if let Some(previous) = out.last_mut() {
            if previous.r#ref == row.r#ref
                && previous.surface == row.surface
                && previous.english.mapping == Mapping::Partial
            {
                previous.english_alternatives.push(english);
                continue;
            }
        }
        out.push(Occurrence {
            r#ref: row.r#ref,
            book: row.book,
            chapter: row.chapter,
            verse: row.verse,
            english,
            surface: row.surface,
            lemma: row.lemma,
            morph: row.morph,
            prefixes: row.prefixes,
            homograph: row.homograph,
            from_qere: row.from_qere,
            english_alternatives: Vec::new(),
        });
    }
    out
}

/// A bound parameter value for the SQL builders below. The builders emit
/// `$N` placeholders; the `repos` pass binds each value positionally in
/// [`BuiltQuery::params`] order. No `sqlx` dependency here so these stay
/// unit-testable without a database.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WhereParam {
    Text(String),
    Int(i32),
}

/// A parameterised statement: SQL text with `$N` placeholders plus the
/// values in placeholder order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BuiltQuery {
    pub sql: String,
    pub params: Vec<(String, WhereParam)>,
}

/// The shared predicate, so occurrences and aggregates cannot diverge.
///
/// Returns the `WHERE` body (without the keyword) with `$N` placeholders
/// numbered from 1, plus `(name, value)` params in placeholder order.
pub fn build_where(q: &LemmaQuery) -> (String, Vec<(String, WhereParam)>) {
    let mut clauses = vec!["w.language = $1".to_string(), "w.strong = $2".to_string()];
    let mut params: Vec<(String, WhereParam)> = vec![
        ("language".to_string(), WhereParam::Text(q.language.clone())),
        ("strong".to_string(), WhereParam::Text(q.strong.clone())),
    ];
    // Placeholder counter: next index to assign.
    let mut next = params.len() + 1;

    // The empty string is explicitly "the rows Strong's did not split",
    // which is a different question from "every row under this number".
    if let Some(homograph) = &q.homograph {
        if homograph.is_empty() {
            clauses.push("w.homograph IS NULL".to_string());
        } else {
            clauses.push(format!("w.homograph = ${next}"));
            params.push(("homograph".to_string(), WhereParam::Text(homograph.clone())));
            next += 1;
        }
    }

    if let Some(book) = &q.book {
        clauses.push(format!("split_part(w.ref, '.', 1) = ${next}"));
        params.push(("book".to_string(), WhereParam::Text(book.clone())));
        next += 1;
    }
    if let Some(start) = q.chapter_start {
        clauses.push(format!("split_part(w.ref, '.', 2)::int >= ${next}"));
        params.push(("chapter_start".to_string(), WhereParam::Int(start)));
        next += 1;
    }
    if let Some(end) = q.chapter_end {
        clauses.push(format!("split_part(w.ref, '.', 2)::int <= ${next}"));
        params.push(("chapter_end".to_string(), WhereParam::Int(end)));
        next += 1;
    }
    let _ = next;
    (clauses.join(" AND "), params)
}

/// One row per word, ordered canonically, carrying no span at all.
///
/// The English reference is a LEFT JOIN, so a verse the two traditions
/// agree on comes back with no mapping row — which is why `map_loaded` has
/// to be passed to [`fold_occurrences`] rather than inferred from the
/// absence of a row (see [`english_reference`]). The join is on
/// `from_ref` alone rather than on the part, so a partial produces one row
/// per half and the caller sees that the verse is split instead of
/// silently receiving whichever half sorted first.
///
/// The scheme join keys extend the `WHERE` params: with `k` where-params,
/// `vm.from_scheme` is `$k+1` and `vm.to_scheme` is `$k+2`, appended in
/// that order.
pub fn occurrences_query(q: &LemmaQuery) -> BuiltQuery {
    let (where_clause, mut params) = build_where(q);
    let from_placeholder = params.len() + 1;
    let to_placeholder = params.len() + 2;
    params.push((
        "from_scheme".to_string(),
        WhereParam::Text(HEBREW.to_string()),
    ));
    params.push((
        "to_scheme".to_string(),
        WhereParam::Text(ENGLISH.to_string()),
    ));
    BuiltQuery {
        sql: format!(
            "SELECT w.ref, \
             split_part(w.ref, '.', 1) AS book, \
             split_part(w.ref, '.', 2)::int AS chapter, \
             split_part(w.ref, '.', 3)::int AS verse, \
             w.surface, w.lemma, w.morph, w.prefixes, w.homograph, w.from_qere, \
             vm.to_ref, vm.to_part, vm.from_part, vm.mapping_type, \
             COALESCE(b.ordinal, 999) AS ordinal \
             FROM core.words w \
             LEFT JOIN core.edition_books b \
             ON b.edition_key = 'WLC' \
             AND b.osis_id = split_part(w.ref, '.', 1) \
             LEFT JOIN core.verse_map vm \
             ON vm.from_scheme = ${from_placeholder} \
             AND vm.to_scheme = ${to_placeholder} \
             AND vm.from_ref = w.ref \
             WHERE {where_clause} \
             ORDER BY ordinal, chapter, verse, w.document_id, w.position, \
             vm.from_part NULLS FIRST"
        ),
        params,
    }
}

/// Which aggregate a [`AggregateQuery`] counts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AggregateKey {
    BySurface,
    ByBook,
    ByMorph,
    ByPrefixes,
    ByHomograph,
}

/// One aggregate statement: what it counts, its SQL, and its params (the
/// shared `WHERE` params, in placeholder order).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregateQuery {
    pub key: AggregateKey,
    pub sql: String,
    pub params: Vec<(String, WhereParam)>,
}

/// The counts a lexicographic survey actually reports.
///
/// `prefixes` is counted, not normalised away: `k/4941` is 37 occurrences
/// of "according to the *mishpat* of" and `b/4941` is 33. Those are
/// findings about idiom, and a query that stripped them to compare bare
/// lemmas would delete the result.
///
/// Order matches the Python dict: surface, book, morph, prefixes,
/// homograph.
pub fn aggregate_queries(q: &LemmaQuery) -> Vec<AggregateQuery> {
    // (key, SELECT expression): NULL prefixes/homographs count under ''.
    const GROUPS: [(AggregateKey, &str); 4] = [
        (AggregateKey::BySurface, "w.surface"),
        (AggregateKey::ByMorph, "w.morph"),
        (AggregateKey::ByPrefixes, "COALESCE(w.prefixes, '')"),
        (AggregateKey::ByHomograph, "COALESCE(w.homograph, '')"),
    ];
    let (where_clause, params) = build_where(q);
    let mut queries: Vec<AggregateQuery> = GROUPS
        .iter()
        .map(|(key, expression)| AggregateQuery {
            key: *key,
            sql: format!(
                "SELECT {expression} AS value, count(*) AS n \
                 FROM core.words w WHERE {where_clause} \
                 GROUP BY 1 ORDER BY n DESC, 1"
            ),
            params: params.clone(),
        })
        .collect();
    // `by_book` is ordered canonically by edition ordinal, not by count.
    let (book_where, book_params) = build_where(q);
    queries.insert(
        1,
        AggregateQuery {
            key: AggregateKey::ByBook,
            sql: format!(
                "SELECT split_part(w.ref, '.', 1) AS value, count(*) AS n, \
                 COALESCE(b.ordinal, 999) AS ordinal \
                 FROM core.words w \
                 LEFT JOIN core.edition_books b \
                 ON b.edition_key = 'WLC' \
                 AND b.osis_id = split_part(w.ref, '.', 1) \
                 WHERE {book_where} \
                 GROUP BY 1, 3 ORDER BY ordinal"
            ),
            params: book_params,
        },
    );
    queries
}

/// The OSIS book ids `core.words` actually holds, in canonical order.
pub fn known_books_query(language: &str) -> BuiltQuery {
    BuiltQuery {
        sql: "SELECT DISTINCT split_part(w.ref, '.', 1) AS osis_id, \
              min(COALESCE(b.ordinal, 999)) AS ordinal \
              FROM core.words w \
              LEFT JOIN core.edition_books b \
              ON b.osis_id = split_part(w.ref, '.', 1) \
              AND b.edition_key = 'WLC' \
              WHERE w.language = $1 \
              GROUP BY 1 ORDER BY 2, 1"
            .to_string(),
        params: vec![(
            "language".to_string(),
            WhereParam::Text(language.to_string()),
        )],
    }
}

/// Whether there is a Hebrew-to-English map to consult at all.
///
/// Coarse on purpose: it separates "migrated but never loaded" — the
/// failure that actually happens, and the one that used to be silent —
/// from a working map. A partially loaded map is not detectable here and
/// is the integration suite's job, which asserts the exact row count.
pub fn verse_map_is_loaded_query() -> BuiltQuery {
    BuiltQuery {
        sql: "SELECT 1 FROM core.verse_map \
              WHERE from_scheme = $1 AND to_scheme = $2 LIMIT 1"
            .to_string(),
        params: vec![
            ("src".to_string(), WhereParam::Text(HEBREW.to_string())),
            ("dst".to_string(), WhereParam::Text(ENGLISH.to_string())),
        ],
    }
}

/// One surface spelling and how many occurrences carry it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SurfaceCount {
    pub surface: String,
    pub count: i64,
}

/// One book and how many occurrences fall in it (canonical order).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BookCount {
    pub book: String,
    pub count: i64,
}

/// One morphology tag and how many occurrences carry it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MorphCount {
    pub morph: String,
    pub count: i64,
}

/// One prefix bundle (`''` for bare forms) and its count.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PrefixCount {
    pub prefixes: String,
    pub count: i64,
}

/// One homograph letter (`''` for unsplit rows) and its count.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HomographCount {
    pub homograph: String,
    pub count: i64,
}

/// The counts a lexicographic survey actually reports.
///
/// Wire shape: Python's zero-result `LemmaResult` carries `counts = {}`
/// (the dataclass default is never replaced on the miss path), while a hit
/// carries all five keys. An all-empty `AggregateCounts` therefore
/// serializes as `{}`, not as five empty arrays; deserialization accepts
/// both (missing keys default to empty).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AggregateCounts {
    #[serde(default)]
    pub by_surface: Vec<SurfaceCount>,
    #[serde(default)]
    pub by_book: Vec<BookCount>,
    #[serde(default)]
    pub by_morph: Vec<MorphCount>,
    #[serde(default)]
    pub by_prefixes: Vec<PrefixCount>,
    #[serde(default)]
    pub by_homograph: Vec<HomographCount>,
}

impl AggregateCounts {
    /// True when every aggregate is empty — the zero-result wire shape.
    pub fn is_empty(&self) -> bool {
        self.by_surface.is_empty()
            && self.by_book.is_empty()
            && self.by_morph.is_empty()
            && self.by_prefixes.is_empty()
            && self.by_homograph.is_empty()
    }
}

/// Serialize [`AggregateCounts`]: `{}` when empty (the Python zero-result
/// shape), the derived map otherwise.
///
/// `collect_map` rather than `serialize_map(...)?` + `.end()`: the latter
/// leaves a `?` error arm inside this generic hook, and every serializer
/// instantiation then needs its own fault to cover the same arm (a failing
/// test serializer poisons the measurement more than it proves). `collect_map`
/// returns the serializer's result directly — same values, no caller arm.
fn serialize_counts<S>(
    counts: &AggregateCounts,
    serializer: S,
) -> std::result::Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    if counts.is_empty() {
        serializer.collect_map(std::iter::empty::<(String, String)>())
    } else {
        // The derived `Serialize` impl (this `serialize_with` hook only runs
        // via the containing struct's derive, so this is not recursive).
        serde::Serialize::serialize(counts, serializer)
    }
}

/// The result of a lemma lookup: verse-identity occurrences plus the
/// aggregates that describe the whole result set even when the caller asked
/// not to enumerate it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LemmaResult {
    pub query: QueryEcho,
    pub total: i64,
    pub books: i64,
    pub occurrences: Vec<Occurrence>,
    #[serde(serialize_with = "serialize_counts")]
    pub counts: AggregateCounts,
    pub notes: Vec<String>,
}

impl LemmaResult {
    /// The zero-result shape: no occurrences, no counts, and the hint.
    pub fn zero(query: &LemmaQuery) -> Self {
        Self {
            query: query.echo(),
            total: 0,
            books: 0,
            occurrences: Vec::new(),
            counts: AggregateCounts::default(),
            notes: vec![zero_result_note(query)],
        }
    }
}

/// The zero-result hint: which number in which lexicon missed, and how to
/// widen. A set homograph letter is echoed; an absent or empty one adds no
/// suffix (the empty string already asked for unsplit rows only).
pub fn zero_result_note(q: &LemmaQuery) -> String {
    let mut note = format!("No word in '{}' carries Strong's {}", q.language, q.strong);
    if let Some(homograph) = &q.homograph {
        if !homograph.is_empty() {
            note.push_str(&format!(" with homograph '{homograph}'"));
        }
    }
    note.push_str(". Check the number, or drop the homograph to widen.");
    note
}

/// First in the notes list when the map is empty: every English reference
/// below is withheld because of this, and a caller that reads one note
/// reads this. Includes the runbook: migration 016 creates the tables but
/// does not fill them.
pub fn map_empty_note() -> &'static str {
    "core.verse_map is empty, so no English-tradition reference could be \
     resolved and every occurrence reports mapping='unmapped'. The Hebrew \
     references are unaffected and remain citable in LHB and WLC. Run \
     `uv run python scripts/load_versification.py` to load the 1,978 \
     mappings; migration 016 creates the tables but does not fill them."
}

/// Refusal past [`MAX_OCCURRENCES`]: narrow, or read the counts instead.
pub fn over_limit_note(total: i64) -> String {
    format!(
        "{total} occurrences is over the {MAX_OCCURRENCES} limit; narrow with \
         book or chapters, or read counts instead. No occurrences returned."
    )
}

/// Attached when partials are present: the English ref is the verse the
/// text begins in, and the part says which half.
pub fn partials_note(count: usize) -> String {
    format!(
        "{count} occurrence(s) sit in a verse the English tradition splits or \
         joins; their english.ref is the verse the text begins in, and \
         english.part says which half. Cite the Hebrew reference unless you \
         are quoting an English edition."
    )
}

/// Attached when qere-sourced occurrences are present: the reference is
/// right in every edition, but a ketiv-printing edition writes a different
/// word there. Lists the first 6 refs, then an ellipsis.
pub fn qere_note(refs: &[String]) -> String {
    let mut listed = refs
        .iter()
        .take(6)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(", ");
    if refs.len() > 6 {
        listed.push_str(", …");
    }
    format!(
        "{} occurrence(s) come from a qere ({listed}). The reference is right \
         in every edition, but an edition that prints the ketiv writes a \
         different word there — read the verse before quoting the surface form.",
        refs.len()
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mapping_type_rejects_unknown_values() {
        // The `other` arm: refusal contract, byte-identical message.
        let err = Mapping::from_mapping_type("sideways").unwrap_err();
        assert_eq!(
            err.to_string(),
            "invalid query: unknown verse_map mapping_type: \"sideways\""
        );
        assert_eq!(Mapping::from_mapping_type("full").unwrap(), Mapping::Full);
        assert_eq!(
            Mapping::from_mapping_type("partial").unwrap(),
            Mapping::Partial
        );
    }

    // Mirrors tests/unit/test_english_reference.py case-for-case: `unmapped`
    // is not `same`, and the difference is the whole of this module.

    mod no_map_loaded {
        use super::*;

        #[test]
        fn verse_reports_unmapped_rather_than_same() {
            assert_eq!(
                english_reference("Gen.18.19", None, None, None, None, false),
                EnglishRef {
                    r#ref: None,
                    mapping: Mapping::Unmapped,
                    part: None,
                    hebrew_part: None,
                }
            );
        }

        #[test]
        fn english_reference_is_withheld_not_guessed() {
            // Echoing the Hebrew reference back would be a claim, and a
            // wrong one: Ps.36.6 is Ps.36.5 in English. Returning Ps.36.6
            // as the English reference because no row was found is precisely
            // the failure.
            assert_eq!(
                english_reference("Ps.36.6", None, None, None, None, false).r#ref,
                None
            );
        }

        #[test]
        fn it_does_not_matter_what_the_row_said() {
            // Nothing can be mapped when there is no map, even if a row
            // leaks in.
            assert_eq!(
                english_reference(
                    "Ps.36.6",
                    Some("Ps.36.5"),
                    None,
                    None,
                    Some(Mapping::Full),
                    false
                )
                .mapping,
                Mapping::Unmapped
            );
        }
    }

    mod map_loaded {
        use super::*;

        #[test]
        fn verse_with_no_row_is_genuinely_the_same_verse() {
            assert_eq!(
                english_reference("Gen.18.19", None, None, None, None, true),
                EnglishRef {
                    r#ref: Some("Gen.18.19".to_string()),
                    mapping: Mapping::Same,
                    part: None,
                    hebrew_part: None,
                }
            );
        }

        #[test]
        fn moved_verse_reports_where_it_moved() {
            assert_eq!(
                english_reference(
                    "Ps.36.6",
                    Some("Ps.36.5"),
                    None,
                    None,
                    Some(Mapping::Full),
                    true
                ),
                EnglishRef {
                    r#ref: Some("Ps.36.5".to_string()),
                    mapping: Mapping::Full,
                    part: None,
                    hebrew_part: None,
                }
            );
        }

        #[test]
        fn partial_keeps_both_halves() {
            // Isa.63.19!b -> Isa.64.1: the half is the reason it is not full.
            assert_eq!(
                english_reference(
                    "Isa.63.19",
                    Some("Isa.64.1"),
                    None,
                    Some("b"),
                    Some(Mapping::Partial),
                    true
                ),
                EnglishRef {
                    r#ref: Some("Isa.64.1".to_string()),
                    mapping: Mapping::Partial,
                    part: None,
                    hebrew_part: Some("b".to_string()),
                }
            );
        }
    }

    #[test]
    fn three_outcomes_are_distinguishable() {
        // A caller must be able to tell all three apart from `mapping` alone.
        let outcomes = [
            english_reference("Gen.1.1", None, None, None, None, false).mapping,
            english_reference("Gen.1.1", None, None, None, None, true).mapping,
            english_reference(
                "Ps.36.6",
                Some("Ps.36.5"),
                None,
                None,
                Some(Mapping::Full),
                true,
            )
            .mapping,
        ]
        .into_iter()
        .collect::<std::collections::HashSet<_>>();
        assert_eq!(
            outcomes,
            [Mapping::Unmapped, Mapping::Same, Mapping::Full]
                .into_iter()
                .collect()
        );
    }

    // WHERE-builder cases: homograph three states, book/chapter filters,
    // param ordering.

    fn text(name: &str, value: &str) -> (String, WhereParam) {
        (name.to_string(), WhereParam::Text(value.to_string()))
    }

    #[test]
    fn where_bare_strong_names_language_and_number() {
        let (where_clause, params) = build_where(&LemmaQuery::new("4941"));
        assert_eq!(where_clause, "w.language = $1 AND w.strong = $2");
        assert_eq!(params, vec![text("language", "he"), text("strong", "4941")]);
    }

    #[test]
    fn where_absent_homograph_asks_every_homograph() {
        let q = LemmaQuery {
            homograph: None,
            ..LemmaQuery::new("4941")
        };
        let (where_clause, params) = build_where(&q);
        assert!(!where_clause.contains("homograph"));
        assert_eq!(params.len(), 2);
    }

    #[test]
    fn where_letter_homograph_selects_that_one() {
        let q = LemmaQuery {
            homograph: Some("a".to_string()),
            ..LemmaQuery::new("4941")
        };
        let (where_clause, params) = build_where(&q);
        assert_eq!(
            where_clause,
            "w.language = $1 AND w.strong = $2 AND w.homograph = $3"
        );
        assert_eq!(
            params,
            vec![
                text("language", "he"),
                text("strong", "4941"),
                text("homograph", "a"),
            ]
        );
    }

    #[test]
    fn where_empty_homograph_means_only_rows_without_a_letter() {
        // A different question from "every row under this number": no param,
        // an IS NULL clause instead.
        let q = LemmaQuery {
            homograph: Some(String::new()),
            ..LemmaQuery::new("4941")
        };
        let (where_clause, params) = build_where(&q);
        assert_eq!(
            where_clause,
            "w.language = $1 AND w.strong = $2 AND w.homograph IS NULL"
        );
        assert_eq!(params, vec![text("language", "he"), text("strong", "4941")]);
    }

    #[test]
    fn where_book_and_chapter_filters_number_placeholders_in_order() {
        let q = LemmaQuery {
            book: Some("Ps".to_string()),
            chapter_start: Some(1),
            chapter_end: Some(50),
            homograph: Some("a".to_string()),
            ..LemmaQuery::new("4941")
        };
        let (where_clause, params) = build_where(&q);
        assert_eq!(
            where_clause,
            "w.language = $1 AND w.strong = $2 AND w.homograph = $3 \
             AND split_part(w.ref, '.', 1) = $4 \
             AND split_part(w.ref, '.', 2)::int >= $5 \
             AND split_part(w.ref, '.', 2)::int <= $6"
        );
        assert_eq!(
            params,
            vec![
                text("language", "he"),
                text("strong", "4941"),
                text("homograph", "a"),
                text("book", "Ps"),
                ("chapter_start".to_string(), WhereParam::Int(1)),
                ("chapter_end".to_string(), WhereParam::Int(50)),
            ]
        );
    }

    #[test]
    fn where_chapter_start_alone_still_numbers_from_three() {
        let q = LemmaQuery {
            chapter_start: Some(36),
            ..LemmaQuery::new("6666")
        };
        let (where_clause, params) = build_where(&q);
        assert_eq!(
            where_clause,
            "w.language = $1 AND w.strong = $2 \
             AND split_part(w.ref, '.', 2)::int >= $3"
        );
        assert_eq!(
            params,
            vec![
                text("language", "he"),
                text("strong", "6666"),
                ("chapter_start".to_string(), WhereParam::Int(36)),
            ]
        );
    }

    // Occurrence-fold cases: partial halves fold, non-partial duplicates do
    // not.

    fn row(hebrew_ref: &str, surface: &str) -> WordRow {
        WordRow {
            r#ref: hebrew_ref.to_string(),
            book: hebrew_ref.split('.').next().unwrap_or("").to_string(),
            chapter: 63,
            verse: 19,
            surface: surface.to_string(),
            lemma: "lemma".to_string(),
            morph: "morph".to_string(),
            prefixes: None,
            homograph: None,
            from_qere: false,
            to_ref: None,
            to_part: None,
            from_part: None,
            mapping: None,
        }
    }

    fn partial_row(hebrew_ref: &str, surface: &str, from_part: &str, to: &str) -> WordRow {
        WordRow {
            to_ref: Some(to.to_string()),
            from_part: Some(from_part.to_string()),
            mapping: Some(Mapping::Partial),
            ..row(hebrew_ref, surface)
        }
    }

    #[test]
    fn partial_halves_fold_onto_one_occurrence() {
        let rows = vec![
            partial_row("Isa.63.19", "surface", "a", "Isa.63.19"),
            partial_row("Isa.63.19", "surface", "b", "Isa.64.1"),
        ];
        let folded = fold_occurrences(rows, true);
        assert_eq!(folded.len(), 1);
        assert_eq!(folded[0].english.mapping, Mapping::Partial);
        assert_eq!(folded[0].english.hebrew_part, Some("a".to_string()));
        assert_eq!(folded[0].english_alternatives.len(), 1);
        assert_eq!(
            folded[0].english_alternatives[0].r#ref,
            Some("Isa.64.1".to_string())
        );
        assert_eq!(
            folded[0].english_alternatives[0].hebrew_part,
            Some("b".to_string())
        );
    }

    #[test]
    fn non_partial_duplicates_do_not_fold() {
        // Distinct words sharing a surface stay distinct occurrences.
        let rows = vec![
            WordRow {
                to_ref: Some("Ps.36.5".to_string()),
                mapping: Some(Mapping::Full),
                ..row("Ps.36.6", "surface")
            },
            WordRow {
                to_ref: Some("Ps.36.5".to_string()),
                mapping: Some(Mapping::Full),
                ..row("Ps.36.6", "surface")
            },
        ];
        let folded = fold_occurrences(rows, true);
        assert_eq!(folded.len(), 2);
        assert!(folded.iter().all(|o| o.english_alternatives.is_empty()));
    }

    #[test]
    fn unmapped_duplicates_do_not_fold() {
        let folded = fold_occurrences(
            vec![row("Gen.1.1", "surface"), row("Gen.1.1", "surface")],
            false,
        );
        assert_eq!(folded.len(), 2);
        assert!(folded
            .iter()
            .all(|o| o.english.mapping == Mapping::Unmapped));
    }

    #[test]
    fn same_verse_different_surface_does_not_fold() {
        let rows = vec![
            partial_row("Isa.63.19", "one", "a", "Isa.63.19"),
            partial_row("Isa.63.19", "other", "b", "Isa.64.1"),
        ];
        let folded = fold_occurrences(rows, true);
        assert_eq!(folded.len(), 2);
    }

    #[test]
    fn fold_requires_the_previous_mapping_to_be_partial() {
        // A full row followed by a partial half with the same verse and
        // surface is not a pair of halves: the guard is on the previous
        // occurrence, not the incoming row.
        let rows = vec![
            WordRow {
                to_ref: Some("Isa.63.19".to_string()),
                mapping: Some(Mapping::Full),
                ..row("Isa.63.19", "surface")
            },
            partial_row("Isa.63.19", "surface", "b", "Isa.64.1"),
        ];
        let folded = fold_occurrences(rows, true);
        assert_eq!(folded.len(), 2);
        assert_eq!(folded[0].english.mapping, Mapping::Full);
        assert_eq!(folded[1].english.mapping, Mapping::Partial);
    }

    // SQL shapes (the integration suites assert the rows; these pin the text
    // the repos pass will execute).

    #[test]
    fn occurrences_sql_appends_scheme_params_after_where_params() {
        let q = LemmaQuery::new("4941");
        let built = occurrences_query(&q);
        assert!(built.sql.contains("FROM core.words w"));
        assert!(built.sql.contains("LEFT JOIN core.edition_books b"));
        assert!(built.sql.contains("LEFT JOIN core.verse_map vm"));
        assert!(built.sql.contains("vm.from_scheme = $3"));
        assert!(built.sql.contains("vm.to_scheme = $4"));
        assert!(built.sql.contains("AND vm.from_ref = w.ref"));
        assert!(built
            .sql
            .contains("WHERE w.language = $1 AND w.strong = $2"));
        assert!(built.sql.contains(
            "ORDER BY ordinal, chapter, verse, w.document_id, w.position, vm.from_part NULLS FIRST"
        ));
        assert_eq!(
            built.params,
            vec![
                text("language", "he"),
                text("strong", "4941"),
                text("from_scheme", "hebrew"),
                text("to_scheme", "english"),
            ]
        );
    }

    #[test]
    fn aggregate_queries_keep_coalesce_group_and_order_shapes() {
        let queries = aggregate_queries(&LemmaQuery::new("4941"));
        let keys = queries.iter().map(|query| query.key).collect::<Vec<_>>();
        assert_eq!(
            keys,
            vec![
                AggregateKey::BySurface,
                AggregateKey::ByBook,
                AggregateKey::ByMorph,
                AggregateKey::ByPrefixes,
                AggregateKey::ByHomograph,
            ]
        );
        for query in &queries {
            assert!(query.sql.contains("FROM core.words w"), "{}", query.sql);
            assert_eq!(query.params.len(), 2);
        }
        let by_prefixes = &queries[3];
        assert!(by_prefixes.sql.contains("COALESCE(w.prefixes, '')"));
        assert!(by_prefixes.sql.contains("GROUP BY 1 ORDER BY n DESC, 1"));
        let by_homograph = &queries[4];
        assert!(by_homograph.sql.contains("COALESCE(w.homograph, '')"));
        let by_book = &queries[1];
        assert!(by_book.sql.contains("COALESCE(b.ordinal, 999) AS ordinal"));
        assert!(by_book.sql.contains("GROUP BY 1, 3 ORDER BY ordinal"));
    }

    #[test]
    fn known_books_sql_orders_by_ordinal_then_id() {
        let built = known_books_query("he");
        assert!(built.sql.contains("GROUP BY 1 ORDER BY 2, 1"));
        assert!(built.sql.contains("WHERE w.language = $1"));
        assert_eq!(
            built.params,
            vec![("language".to_string(), WhereParam::Text("he".to_string()))]
        );
    }

    #[test]
    fn verse_map_loaded_sql_probes_the_hebrew_to_english_pair() {
        let built = verse_map_is_loaded_query();
        assert!(built.sql.contains("FROM core.verse_map"));
        assert!(built.sql.contains("from_scheme = $1 AND to_scheme = $2"));
        assert_eq!(
            built.params,
            vec![
                ("src".to_string(), WhereParam::Text("hebrew".to_string())),
                ("dst".to_string(), WhereParam::Text("english".to_string())),
            ]
        );
    }

    // User-visible notes, byte-identical to the Python.

    #[test]
    fn zero_result_note_names_language_number_and_letter() {
        let q = LemmaQuery {
            homograph: Some("a".to_string()),
            ..LemmaQuery::new("4941")
        };
        assert_eq!(
            zero_result_note(&q),
            "No word in 'he' carries Strong's 4941 with homograph 'a'. Check \
             the number, or drop the homograph to widen."
        );
    }

    #[test]
    fn zero_result_note_without_homograph_has_no_suffix() {
        assert_eq!(
            zero_result_note(&LemmaQuery::new("99999999")),
            "No word in 'he' carries Strong's 99999999. Check the number, or \
             drop the homograph to widen."
        );
    }

    #[test]
    fn zero_result_note_with_empty_homograph_has_no_suffix() {
        // The empty string already asked for unsplit rows only; like the
        // Python falsy check, it adds no "with homograph" clause.
        let q = LemmaQuery {
            homograph: Some(String::new()),
            ..LemmaQuery::new("4941")
        };
        assert!(!zero_result_note(&q).contains("homograph '"));
    }

    #[test]
    fn map_empty_note_carries_the_runbook() {
        assert_eq!(
            map_empty_note(),
            "core.verse_map is empty, so no English-tradition reference could \
             be resolved and every occurrence reports mapping='unmapped'. The \
             Hebrew references are unaffected and remain citable in LHB and \
             WLC. Run `uv run python scripts/load_versification.py` to load \
             the 1,978 mappings; migration 016 creates the tables but does \
             not fill them."
        );
    }

    #[test]
    fn over_limit_note_refuses_rather_than_truncates() {
        assert_eq!(
            over_limit_note(2500),
            "2500 occurrences is over the 2000 limit; narrow with book or \
             chapters, or read counts instead. No occurrences returned."
        );
    }

    #[test]
    fn partials_note_reports_the_split() {
        assert_eq!(
            partials_note(3),
            "3 occurrence(s) sit in a verse the English tradition splits or \
             joins; their english.ref is the verse the text begins in, and \
             english.part says which half. Cite the Hebrew reference unless \
             you are quoting an English edition."
        );
    }

    #[test]
    fn qere_note_lists_refs_and_truncates_at_six() {
        let refs = vec!["Gen.1.1".to_string(), "Ex.2.3".to_string()];
        assert_eq!(
            qere_note(&refs),
            "2 occurrence(s) come from a qere (Gen.1.1, Ex.2.3). The \
             reference is right in every edition, but an edition that prints \
             the ketiv writes a different word there — read the verse before \
             quoting the surface form."
        );
        let many = (1..=8).map(|n| format!("Ref.1.{n}")).collect::<Vec<_>>();
        let note = qere_note(&many);
        assert!(note.starts_with("8 occurrence(s) come from a qere ("));
        assert!(note.contains("Ref.1.6, …)"));
        assert!(!note.contains("Ref.1.7"));
    }

    #[test]
    fn zero_result_shape_carries_query_echo_and_hint() {
        let q = LemmaQuery {
            book: Some("Ps".to_string()),
            chapter_start: Some(1),
            chapter_end: Some(50),
            ..LemmaQuery::new("99999999")
        };
        let result = LemmaResult::zero(&q);
        assert_eq!(result.total, 0);
        assert_eq!(result.books, 0);
        assert!(result.occurrences.is_empty());
        assert_eq!(
            result.query,
            QueryEcho {
                strong: "99999999".to_string(),
                language: "he".to_string(),
                homograph: None,
                book: Some("Ps".to_string()),
                chapters: Some([Some(1), Some(50)]),
            }
        );
        assert_eq!(result.notes.len(), 1);
        assert!(result.notes[0].contains("99999999"));
    }

    #[test]
    fn empty_counts_serialize_as_empty_map() {
        // Python's zero-result `LemmaResult` carries `counts = {}`; a hit
        // carries all five aggregate keys.
        let zero = LemmaResult::zero(&LemmaQuery::new("99999999"));
        let value = serde_json::to_value(&zero).expect("serializes");
        assert_eq!(
            value.get("counts"),
            Some(&serde_json::Value::Object(serde_json::Map::new()))
        );
        // Deserialization accepts both the empty map and the full shape.
        let back: LemmaResult = serde_json::from_value(value).expect("round-trips");
        assert_eq!(back, zero);
    }

    #[test]
    fn query_echo_has_null_chapters_without_bounds() {
        assert_eq!(LemmaQuery::new("4941").echo().chapters, None);
    }

    #[test]
    fn non_empty_counts_serialize_with_all_five_keys() {
        // The hit shape: all five aggregate keys present (contrast
        // `counts = {}` on the miss path).
        let counts = AggregateCounts {
            by_surface: vec![SurfaceCount {
                surface: "x".to_string(),
                count: 1,
            }],
            ..AggregateCounts::default()
        };
        let result = LemmaResult {
            query: LemmaQuery::new("4941").echo(),
            total: 1,
            books: 1,
            occurrences: Vec::new(),
            counts,
            notes: Vec::new(),
        };
        let value = serde_json::to_value(&result).expect("serializes");
        let map = value
            .get("counts")
            .expect("counts")
            .as_object()
            .expect("map");
        for key in [
            "by_surface",
            "by_book",
            "by_morph",
            "by_prefixes",
            "by_homograph",
        ] {
            assert!(map.contains_key(key), "{map:?}");
        }
        let back: LemmaResult = serde_json::from_value(value).expect("round-trips");
        assert_eq!(back, result);
    }
}
