//! `scripts/ingest_wlc.py` (pure parts): WLC document records.
//!
//! The database orchestration stays Python; what ports is the record
//! building: the LHB title-suffix rule, NFC normalization of the rendered
//! chapter (morphhb writes non-canonical mark order, LHB is canonical, and
//! the quote matcher folds per character rather than reordering), the
//! document title/source strings, and the metadata with its ketiv/qere
//! pairs. Takes an already-rendered chapter on stdin-adjacent files and
//! prints the document record as JSON.

use std::collections::HashMap;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{json, Value};
use unicode_normalization::UnicodeNormalization;

const EDITION_KEY: &str = "WLC";
const TITLE_PREFIX: &str = "Westminster Leningrad Codex";
const LHB_TITLE_PREFIX: &str = "Lexham Hebrew Bible — ";
const RESOURCE_ID: &str = "openscriptures/morphhb";
const SOURCE_SCHEME: &str = "openscriptures";

/// LHB's Logos book codes to morphhb's OSIS filenames, in canonical order
/// (shared with `load_versification`'s book rows; the order is the ordinal).
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

static CHAPTER_SUFFIX: LazyLock<Regex> = LazyLock::new(|| Regex::new(r" \d+(?:\n\z|\z)").unwrap());

/// The `(code, chapter)` suffix of an LHB title: the title past the prefix,
/// with the chapter number restored for one-chapter books (Obadiah's title
/// carries none, WLC titles are uniformly `<Book> <Chapter>`).
fn title_suffix(lhb_title: &str, chapter: u64) -> String {
    let suffix = lhb_title
        .strip_prefix(LHB_TITLE_PREFIX)
        .unwrap_or(lhb_title);
    if CHAPTER_SUFFIX.is_match(suffix) {
        suffix.to_owned()
    } else {
        format!("{suffix} {chapter}")
    }
}

fn build_metadata(
    code: &str,
    chapter: u64,
    osis_book: &str,
    commit: &str,
    retrieved: &str,
    kq: &[(u64, String, String)],
) -> Value {
    json!({
        "article": format!("{EDITION_KEY}.{code}.{chapter}"),
        "language": "he",
        "edition_key": EDITION_KEY,
        "resource_id": RESOURCE_ID,
        "osis_id": format!("{osis_book}.{chapter}"),
        "provenance": {
            "repo": "https://github.com/openscriptures/morphhb",
            "commit": commit,
            "file": format!("wlc/{osis_book}.xml"),
            "retrieved": retrieved,
            "rights": "public domain (WLC text); morphhb markup CC-BY-4.0",
            "unicode_normalization": "NFC",
            "qere_policy": "qere-in-text",
        },
        "ketiv_qere": kq.iter().map(|(verse, ketiv, qere)| {
            json!({"verse": verse, "ketiv": ketiv, "qere": qere})
        }).collect::<Vec<_>>(),
    })
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
    if args.iter().any(|a| a == "--suffix") {
        // The LHB title-suffix rule on its own.
        let title = get("--lhb-title");
        let chapter: u64 = get("--chapter").parse().unwrap();
        println!("{}", title_suffix(&title, chapter));
        return;
    }
    let code = get("--code");
    let chapter: u64 = get("--chapter").parse().unwrap();
    let rendered = std::fs::read_to_string(get("--rendered")).unwrap();
    let kq_raw = std::fs::read_to_string(get("--kq")).unwrap();
    let kq: Vec<(u64, String, String)> = serde_json::from_str::<Vec<Value>>(&kq_raw)
        .unwrap()
        .iter()
        .map(|pair| {
            (
                pair["verse"].as_u64().unwrap(),
                pair["ketiv"].as_str().unwrap().to_owned(),
                pair["qere"].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    let osis_book = LHB_TO_OSIS
        .iter()
        .find(|(c, _)| *c == code)
        .map(|(_, osis)| *osis)
        .unwrap_or_else(|| {
            eprintln!("unknown code {code}");
            std::process::exit(1);
        });
    let suffix = title_suffix(&get("--lhb-title"), chapter);
    // NFC is what makes this comparable to LHB at all: morphhb writes
    // dagesh before sheva and shin-dot before hiriq, which is not canonical
    // order.
    let full_text: String = rendered.nfc().collect();
    let title = format!("{TITLE_PREFIX} \u{2014} {suffix}");
    let source = format!("{SOURCE_SCHEME}:{RESOURCE_ID}:{EDITION_KEY}.{code}.{chapter}");
    let commit = get("--commit");
    let retrieved = get("--retrieved");
    let metadata = build_metadata(&code, chapter, osis_book, &commit, &retrieved, &kq);
    let passage_metadata: HashMap<&str, &Value> =
        ["article", "language", "edition_key", "resource_id"]
            .iter()
            .map(|key| (*key, &metadata[*key]))
            .collect();
    println!(
        "{}",
        json!({
            "title": title,
            "source": source,
            "full_text": full_text,
            "metadata": metadata,
            "passage_metadata": passage_metadata,
        })
    );
}
