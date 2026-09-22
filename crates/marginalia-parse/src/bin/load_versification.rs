//! `scripts/load_versification.py` (pure parts): versification tables.
//!
//! The database writes stay Python; what ports is everything checkable
//! without one: the edition book rows, the verse-map parse, the shape
//! checks, and the corpus-agreement comparison over caller-supplied verse
//! sets. Prints counts and problems, mirroring the script's own output,
//! plus `--dump-rows` for the machine-readable tables.

use std::collections::{HashMap, HashSet};

use marginalia_parse::xml::{parse_xml, TextModel};
use serde_json::{json, Value};

const VERSEMAP_NS: &str = "http://www.APTBibleTools.com/namespace";

/// LHB's Logos book codes to morphhb's OSIS filenames, in canonical order
/// (duplicated from `ingest_wlc`, whose table this is; enumerate gives the
/// ordinal so no second list drifts out of step with the first).
const LHB_TO_OSIS: [(&str, &str); 39] = [
    ("GE", "Gen"),
    ("EX", "Exod"),
    ("LE", "Lev"),
    ("NU", "Num"),
    ("DE", "Deut"),
    ("JOS", "Josh"),
    ("JDG", "Judg"),
    ("RU", "Ruth"),
    ("1SA", "1Sam"),
    ("2SA", "2Sam"),
    ("1KI", "1Kgs"),
    ("2KI", "2Kgs"),
    ("1CH", "1Chr"),
    ("2CH", "2Chr"),
    ("EZR", "Ezra"),
    ("NE", "Neh"),
    ("ES", "Esth"),
    ("JOB", "Job"),
    ("PS", "Ps"),
    ("PR", "Prov"),
    ("ECC", "Eccl"),
    ("SO", "Song"),
    ("IS", "Isa"),
    ("JE", "Jer"),
    ("LA", "Lam"),
    ("EZE", "Ezek"),
    ("DA", "Dan"),
    ("HO", "Hos"),
    ("JOE", "Joel"),
    ("AM", "Amos"),
    ("OB", "Obad"),
    ("JON", "Jonah"),
    ("MIC", "Mic"),
    ("NAH", "Nah"),
    ("HAB", "Hab"),
    ("ZEP", "Zeph"),
    ("HAG", "Hag"),
    ("ZEC", "Zech"),
    ("MAL", "Mal"),
];

/// ESV differs on exactly four Old Testament books: an override on the LHB
/// table rather than a second full table, so the four stay visible.
const ESV_OT_OVERRIDES: [(&str, &str); 4] =
    [("EC", "Eccl"), ("HOS", "Hos"), ("MI", "Mic"), ("NA", "Nah")];
const ESV_OT_DROPPED: [&str; 4] = ["ECC", "HO", "MIC", "NAH"];

const ESV_NT: [(&str, &str); 27] = [
    ("MT", "Matt"),
    ("MK", "Mark"),
    ("LU", "Luke"),
    ("JN", "John"),
    ("AC", "Acts"),
    ("RO", "Rom"),
    ("1CO", "1Cor"),
    ("2CO", "2Cor"),
    ("GA", "Gal"),
    ("EPH", "Eph"),
    ("PHP", "Phil"),
    ("COL", "Col"),
    ("1TH", "1Thess"),
    ("2TH", "2Thess"),
    ("1TI", "1Tim"),
    ("2TI", "2Tim"),
    ("TIT", "Titus"),
    ("PHM", "Phlm"),
    ("HEB", "Heb"),
    ("JAM", "Jas"),
    ("1PE", "1Pet"),
    ("2PE", "2Pet"),
    ("1JN", "1John"),
    ("2JN", "2John"),
    ("3JN", "3John"),
    ("JUD", "Jude"),
    ("REV", "Rev"),
];

/// One `(edition, code)` the corpus actually addresses a book by.
#[derive(Debug, Clone)]
struct BookRow {
    edition_key: String,
    code: String,
    osis_id: String,
    ordinal: u64,
}

fn edition_book_rows() -> Vec<BookRow> {
    let mut rows = Vec::new();
    for edition in ["LHB", "WLC"] {
        for (ordinal, (code, osis)) in LHB_TO_OSIS.iter().enumerate() {
            rows.push(BookRow {
                edition_key: edition.to_owned(),
                code: code.to_string(),
                osis_id: osis.to_string(),
                ordinal: ordinal as u64 + 1,
            });
        }
    }
    let mut esv: Vec<(&str, &str)> = LHB_TO_OSIS
        .iter()
        .filter(|(code, _)| !ESV_OT_DROPPED.contains(code))
        .copied()
        .collect();
    esv.extend(ESV_OT_OVERRIDES);
    esv.extend(ESV_NT);
    for (ordinal, (code, osis)) in esv.iter().enumerate() {
        rows.push(BookRow {
            edition_key: "ESV".to_owned(),
            code: code.to_string(),
            osis_id: osis.to_string(),
            ordinal: ordinal as u64 + 1,
        });
    }
    rows
}

/// Python `repr` of a string: single quotes, backslash escapes. The script
/// formats every problem with `{...!r}`, so byte-identical messages need it.
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
            c if (c as u32) < 0x20 || (c as u32) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('\'');
    out
}

/// Python `repr` of a string list: `['a', 'b']`.
fn py_list(items: &[&str]) -> String {
    format!(
        "[{}]",
        items
            .iter()
            .map(|s| py_repr(s))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

/// Split `"Isa.63.19!b"` into its reference and its half-verse part: the
/// `!a`/`!b` suffix is how the source writes a verse beginning partway
/// through another, kept as its own column so a reader that does not
/// understand parts still sees this row is not a whole-verse equivalence.
fn split_ref(raw: &str) -> (String, Option<String>) {
    match raw.split_once('!') {
        Some((reference, part)) => (reference.to_owned(), Some(part.to_owned())),
        None => (raw.to_owned(), None),
    }
}

#[derive(Debug, Clone)]
struct MapRow {
    from_ref: String,
    to_ref: String,
    from_part: Option<String>,
    to_part: Option<String>,
    mapping_type: String,
    source: String,
}

/// Every mapping in `VerseMap.xml`, as rows. `wlc=` is the Hebrew-scheme
/// reference and `kjv=` the English one.
fn parse_verse_map(raw: &[u8], source: &str) -> Result<Vec<MapRow>, String> {
    let document = parse_xml(raw, TextModel::Et).map_err(|err| err.to_string())?;
    let mut rows = Vec::new();
    for book in document
        .root
        .child_elements()
        .filter(|el| el.tag() == format!("{{{VERSEMAP_NS}}}book"))
    {
        for verse in book
            .child_elements()
            .filter(|el| el.tag() == format!("{{{VERSEMAP_NS}}}verse"))
        {
            let (from_ref, from_part) = split_ref(verse.attr("wlc").unwrap_or(""));
            let (to_ref, to_part) = split_ref(verse.attr("kjv").unwrap_or(""));
            rows.push(MapRow {
                from_ref,
                to_ref,
                from_part,
                to_part,
                mapping_type: verse.attr("type").unwrap_or("full").to_owned(),
                source: source.to_owned(),
            });
        }
    }
    Ok(rows)
}

/// Structural checks on the map, independent of any corpus.
fn check_map_shape(rows: &[MapRow]) -> Vec<String> {
    let mut problems = Vec::new();
    for row in rows {
        if row.mapping_type != "full" && row.mapping_type != "partial" {
            problems.push(format!(
                "unknown mapping type {}",
                py_repr(&row.mapping_type)
            ));
        }
        if row.mapping_type == "full" && (row.from_part.is_some() || row.to_part.is_some()) {
            problems.push(format!("a 'full' mapping carries a part: {}", row.from_ref));
        }
        for side in [&row.from_ref, &row.to_ref] {
            if side.split('.').count() != 3 {
                problems.push(format!(
                    "reference is not book.chapter.verse: {}",
                    py_repr(side)
                ));
            }
        }
    }

    // Every partial is marked as one, and every part-carrier is typed one:
    // the row objects are distinct, so index sets stand in for `id()` sets.
    let partials: HashSet<usize> = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row.mapping_type == "partial")
        .map(|(i, _)| i)
        .collect();
    let parted: HashSet<usize> = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row.from_part.is_some() || row.to_part.is_some())
        .map(|(i, _)| i)
        .collect();
    if partials != parted {
        problems.push(format!(
            "{} rows typed partial but {} carry a part",
            partials.len(),
            parted.len()
        ));
    }

    let mut seen: HashMap<(&str, Option<&str>), Vec<&str>> = HashMap::new();
    for row in rows {
        seen.entry((row.from_ref.as_str(), row.from_part.as_deref()))
            .or_default()
            .push(row.to_ref.as_str());
    }
    for ((from_ref, from_part), targets) in &seen {
        if targets.len() > 1 {
            let key = match from_part {
                Some(part) => format!("({}, {})", py_repr(from_ref), py_repr(part)),
                None => format!("({}, None)", py_repr(from_ref)),
            };
            problems.push(format!(
                "{key} maps to several references: {}",
                py_list(targets)
            ));
        }
    }
    problems
}

/// Check the map verse-by-verse against caller-supplied verse rows: the map
/// is WLC-to-KJV, the corpus holds ESV, and "the ESV follows the KJV
/// tradition" is the assumption under test. Rows arrive as the database
/// holds them — `(code, chapter, verse)` per edition — and resolve to OSIS
/// references through the book table, unresolvable codes failing loudly.
fn validate_against_corpus(
    rows: &[MapRow],
    books: &[BookRow],
    raw: &HashMap<String, Vec<(String, u64, u64)>>,
) -> Vec<String> {
    let mut problems = Vec::new();
    let osis_of: HashMap<(&str, &str), &str> = books
        .iter()
        .map(|b| {
            (
                (b.edition_key.as_str(), b.code.as_str()),
                b.osis_id.as_str(),
            )
        })
        .collect();
    let mut present: HashMap<&str, HashSet<String>> = HashMap::new();
    for (edition, verses) in raw {
        for (code, chapter, verse) in verses {
            match osis_of.get(&(edition.as_str(), code.as_str())) {
                Some(osis) => {
                    present
                        .entry(edition.as_str())
                        .or_default()
                        .insert(format!("{osis}.{chapter}.{verse}"));
                }
                None => problems.push(format!(
                    "{edition} book code {} has no OSIS id",
                    py_repr(code)
                )),
            }
        }
    }

    // 1. Every Hebrew side must exist in both Hebrew editions.
    for edition in ["WLC", "LHB"] {
        let empty = HashSet::new();
        let have = present.get(edition).unwrap_or(&empty);
        let mut missing: Vec<&str> = rows
            .iter()
            .map(|r| r.from_ref.as_str())
            .filter(|r| !have.contains(*r))
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        missing.sort_unstable();
        if !missing.is_empty() {
            let head: Vec<&str> = missing.iter().take(5).copied().collect();
            problems.push(format!(
                "{} mapped Hebrew references absent from {edition}: {}",
                missing.len(),
                py_list(&head)
            ));
        }
    }

    // 2. Every English side must exist in ESV.
    {
        let empty = HashSet::new();
        let have = present.get("ESV").unwrap_or(&empty);
        let mut missing: Vec<&str> = rows
            .iter()
            .map(|r| r.to_ref.as_str())
            .filter(|r| !have.contains(*r))
            .collect::<HashSet<_>>()
            .into_iter()
            .collect();
        missing.sort_unstable();
        if !missing.is_empty() {
            let head: Vec<&str> = missing.iter().take(8).copied().collect();
            problems.push(format!(
                "{} mapped English references absent from ESV: {}",
                missing.len(),
                py_list(&head)
            ));
        }
    }

    // 3. Repairing book identity must surface exactly the books VerseMap
    //    knows about: chapters whose verse counts diverge, both sides.
    let mapped_books: HashSet<&str> = rows
        .iter()
        .map(|r| r.from_ref.split('.').next().unwrap_or(""))
        .collect();
    let mut heb_by_chapter: HashMap<(&str, u64), usize> = HashMap::new();
    let mut eng_by_chapter: HashMap<(&str, u64), usize> = HashMap::new();
    let empty = HashSet::new();
    for reference in present.get("LHB").unwrap_or(&empty) {
        let mut parts = reference.split('.');
        if let (Some(book), Some(chapter)) = (parts.next(), parts.next()) {
            if let Ok(chapter) = chapter.parse::<u64>() {
                *heb_by_chapter.entry((book, chapter)).or_default() += 1;
            }
        }
    }
    for reference in present.get("ESV").unwrap_or(&empty) {
        let mut parts = reference.split('.');
        if let (Some(book), Some(chapter)) = (parts.next(), parts.next()) {
            if let Ok(chapter) = chapter.parse::<u64>() {
                *eng_by_chapter.entry((book, chapter)).or_default() += 1;
            }
        }
    }
    let mut divergent: HashSet<&str> = HashSet::new();
    for (key, count) in &heb_by_chapter {
        if eng_by_chapter.get(key) != Some(count) {
            divergent.insert(key.0);
        }
    }
    if divergent != mapped_books {
        let mut left: Vec<&str> = divergent.iter().copied().collect();
        let mut right: Vec<&str> = mapped_books.iter().copied().collect();
        left.sort_unstable();
        right.sort_unstable();
        problems.push(format!(
            "divergent books {} != VerseMap books {}",
            py_list(&left),
            py_list(&right)
        ));
    }

    problems
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let get = |name: &str| {
        args.iter()
            .position(|a| a == name)
            .and_then(|i| args.get(i + 1).cloned())
            .unwrap_or_else(|| {
                eprintln!("{name} is required");
                std::process::exit(2);
            })
    };
    let books = edition_book_rows();
    if args.iter().any(|a| a == "--dump-books") {
        // Machine modes print only JSON.
        let rows: Vec<Value> = books
            .iter()
            .map(|b| {
                json!({
                    "edition_key": b.edition_key,
                    "code": b.code,
                    "osis_id": b.osis_id,
                    "ordinal": b.ordinal,
                })
            })
            .collect();
        println!("{}", Value::Array(rows));
        return;
    }
    let raw = std::fs::read(get("--verse-map")).unwrap();
    let source = args
        .iter()
        .position(|a| a == "--source")
        .and_then(|i| args.get(i + 1).cloned())
        .unwrap_or_else(|| "test".to_owned());
    let mappings = parse_verse_map(&raw, &source).unwrap_or_else(|err| {
        eprintln!("{err}");
        std::process::exit(1);
    });
    if args.iter().any(|a| a == "--dump-rows") {
        // Machine mode prints only JSON.
        let rows: Vec<Value> = mappings
            .iter()
            .map(|m| {
                json!({
                    "from_scheme": "hebrew",
                    "to_scheme": "english",
                    "from_ref": m.from_ref,
                    "to_ref": m.to_ref,
                    "from_part": m.from_part,
                    "to_part": m.to_part,
                    "mapping_type": m.mapping_type,
                    "source": m.source,
                })
            })
            .collect();
        println!("{}", Value::Array(rows));
        return;
    }
    let partials = mappings
        .iter()
        .filter(|m| m.mapping_type == "partial")
        .count();
    println!(
        "{} book codes, {} verse mappings from {source}",
        books.len(),
        mappings.len()
    );
    println!(
        "  {} full, {partials} partial, {} books",
        mappings.len() - partials,
        mappings
            .iter()
            .map(|m| m.from_ref.split('.').next().unwrap_or(""))
            .collect::<HashSet<_>>()
            .len()
    );
    let problems = check_map_shape(&mappings);
    if !problems.is_empty() {
        println!("REFUSED — the map does not validate:");
        for problem in problems.iter().take(10) {
            println!("  {problem}");
        }
        std::process::exit(1);
    }
    if let Some(i) = args.iter().position(|a| a == "--verses") {
        let verses_raw = std::fs::read_to_string(&args[i + 1]).unwrap();
        let verses: HashMap<String, Vec<(String, u64, u64)>> =
            serde_json::from_str(&verses_raw).unwrap();
        let corpus_problems = validate_against_corpus(&mappings, &books, &verses);
        if !corpus_problems.is_empty() {
            println!("REFUSED — the map does not agree with the corpus:");
            for problem in corpus_problems.iter().take(10) {
                println!("  {problem}");
            }
            std::process::exit(1);
        }
        println!("validated: shape and corpus agreement both clean");
    } else if problems.is_empty() {
        println!("validated: shape clean");
    }
}
