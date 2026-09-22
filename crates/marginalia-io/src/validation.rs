//! Hold a model's output to the schema that asked for it, and anchor its quotes.
//!
//! Python source:
//! `packages/core/src/research_engine/services/extraction/validation.py`.
//!
//! Two jobs, and the second is the point of the whole extraction layer: an
//! extracted claim is only worth storing if you can go back to the sentence it
//! came from. That means every quotation the model returns has to be
//! *located* — turned into offsets into the passage — not merely recognised
//! as present.
//!
//! Offsets returned here always index the original passage text, so
//! `passage[start..end]` is the quotation. An earlier version fell back to
//! searching a whitespace-collapsed copy and returned offsets into *that*,
//! which are silently wrong by however much whitespace preceded the match: a
//! citation that verifies against nothing.
//!
//! Offsets are character offsets, matching Python `str.find` / `match.start()`.
//! The search underneath works in byte offsets; `locate_span` converts on
//! every path (matches always land on character boundaries).

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::sync::LazyLock;

use crate::errors::{Error, Result};
use crate::schemas::{evidence_field_names, is_falsy_value, required_field_names};

const PY_WS_INNER: &str = r"\p{White_Space}\p{Z}\x1c\x1d\x1e\x1f";

// Python `re` `\s` (str), spelled out: White_Space plus Zs/Zl/Zp plus
// U+001C–U+001F. The `regex` crate's `\s` misses those four (Phase 1 finding,
// pinned in `marginalia-text` as `normalize::PY_WS_CLASS`); spell the class
// explicitly so fuzzy matching agrees with CPython 3.13.
// (Pinned class spelled once, above.)

/// Whitespace runs under the Python class above.
static WS_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&format!("[{PY_WS_INNER}]+")).expect("whitespace class is valid"));

/// Python `str.isspace()`: the White_Space property plus U+001C–U+001F.
/// (`\p{Z}` adds nothing the property misses — every Zs/Zl/Zp code point
/// carries White_Space=yes — but the class above spells it anyway, exactly as
/// `marginalia-text` pins it, so the two cannot drift.)
fn is_py_ws(char: char) -> bool {
    char.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&char)
}

/// Collapse whitespace for fuzzy substring matching.
pub fn normalize_whitespace(text: &str) -> String {
    WS_RE
        .replace_all(text, " ")
        .trim_matches(is_py_ws)
        .to_owned()
}

/// One record the model returned, checked and anchored to its passage.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ValidatedRecord {
    pub record_type: String,
    pub fields: Map<String, Value>,
    /// Character offsets of this record's first evidence field within
    /// the passage text. Document-relative offsets are these plus the
    /// passage's own `char_start`, which is why they are stored
    /// passage-relative: a passage with no span of its own still yields a
    /// usable anchor.
    pub evidence_start: usize,
    pub evidence_end: usize,
}

/// Character offsets of `span_text` in `passage_text`, or `None`.
///
/// Python returns character offsets (`str.find`, `match.start()`); the Rust
/// `str::find` / regex hit underneath are byte offsets, so both paths convert
/// (matches always land on character boundaries). Storing byte offsets would
/// agree on ASCII fixtures and diverge past any non-ASCII text — a silent
/// anchor corruption, since these add to the passage `char_start`.
pub fn locate_span(span_text: &str, passage_text: &str) -> Option<(usize, usize)> {
    // A blank quote locates nothing. Without this guard the word list below
    // would be empty and the joined pattern degenerate.
    if !span_text.chars().any(|c| !is_py_ws(c)) {
        return None;
    }

    if let Some(index) = passage_text.find(span_text) {
        let start = passage_text[..index].chars().count();
        return Some((start, start + span_text.chars().count()));
    }

    // `str::split` on the same whitespace class Python's parameterless
    // `split()` uses, so the words — and hence the pattern — agree exactly.
    // Proof there is always at least one word (so no empty guard): the blank
    // guard above returns unless `span_text` holds a character outside
    // `is_py_ws`, and every such character lies outside `[PY_WS_INNER]` —
    // that class is White_Space plus Z plus U+001C–U+001F, and every Z code
    // point carries White_Space — so the split yields a non-empty piece.
    let words: Vec<&str> = WS_RE.split(span_text).filter(|w| !w.is_empty()).collect();
    debug_assert!(!words.is_empty(), "blank quotes return above");
    let pattern = words
        .iter()
        .map(|word| regex::escape(word))
        .collect::<Vec<_>>()
        .join(&format!("[{PY_WS_INNER}]+"));
    let fuzzy = Regex::new(&pattern).ok()?;
    fuzzy.find(passage_text).map(|hit| {
        (
            passage_text[..hit.start()].chars().count(),
            passage_text[..hit.end()].chars().count(),
        )
    })
}

/// Check each record against its declared type and anchor its evidence.
///
/// Raises on the first violation rather than dropping the record: a schema
/// that half-works produces a table that is quietly incomplete, and the
/// caller's one retry exists precisely to give the model the error text and
/// another go.
pub fn validate_records(
    records: &[Value],
    passage_text: &str,
    passage_id: &str,
    record_types: &Map<String, Value>,
) -> Result<Vec<ValidatedRecord>> {
    let empty_definition = Map::new();
    let mut validated = Vec::with_capacity(records.len());
    for (position, record) in records.iter().enumerate() {
        let record_obj = record.as_object();
        let record_type = record_obj
            .and_then(|record| record.get("record_type"))
            .and_then(Value::as_str);
        let Some(record_type) = record_type.filter(|rt| record_types.contains_key(*rt)) else {
            let mut declared: Vec<&String> = record_types.keys().collect();
            declared.sort();
            let declared = if declared.is_empty() {
                "none".to_owned()
            } else {
                declared
                    .iter()
                    .map(|id| id.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            let actual = record_obj.and_then(|record| record.get("record_type"));
            return Err(Error::Validation(format!(
                "Record {position} from passage {passage_id} has record_type {}; the schema declares {declared}.",
                py_repr(actual),
            )));
        };

        let Some(fields) = record_obj
            .and_then(|record| record.get("fields"))
            .and_then(Value::as_object)
        else {
            return Err(Error::Validation(format!(
                "Record {position} from passage {passage_id} has no 'fields' object."
            )));
        };

        let definition = record_types
            .get(record_type)
            .and_then(Value::as_object)
            .unwrap_or(&empty_definition);
        let missing: Vec<String> = required_field_names(definition)
            .into_iter()
            .filter(|name| fields.get(name).is_none_or(is_falsy_value))
            .collect();
        if !missing.is_empty() {
            let missing = missing
                .iter()
                .map(|name| name.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            return Err(Error::Validation(format!(
                "Record {position} of type '{record_type}' from passage {passage_id} \
                is missing required field(s): {missing}."
            )));
        }

        let anchor = anchor(fields, definition, passage_text, passage_id, record_type)?;
        validated.push(ValidatedRecord {
            record_type: record_type.to_owned(),
            fields: fields.clone(),
            evidence_start: anchor.0,
            evidence_end: anchor.1,
        });
    }
    Ok(validated)
}

/// Locate every evidence field, and return the first one's offsets.
///
/// Every declared evidence field is checked, not just the one that becomes the
/// anchor, because an unlocatable quotation in any field means the model
/// invented text — and that record's other fields are no more trustworthy for
/// it having quoted correctly somewhere else.
fn anchor(
    fields: &Map<String, Value>,
    definition: &Map<String, Value>,
    passage_text: &str,
    passage_id: &str,
    record_type: &str,
) -> Result<(usize, usize)> {
    let declared = evidence_field_names(definition);
    if declared.is_empty() {
        return Err(Error::Validation(format!(
            "Record type '{record_type}' declares no 'evidence_span' field, \
            so nothing it produces can be checked against the corpus."
        )));
    }

    let mut located = Vec::new();
    for name in &declared {
        let Some(value) = fields.get(name) else {
            continue;
        };
        // A null evidence field is "not supplied", like a missing one: the
        // all-null check below reports the record, naming every declared
        // field, instead of blaming one field for not being a quotation.
        if value.is_null() {
            continue;
        }
        let Some(quotation) = value.as_str() else {
            return Err(Error::Validation(format!(
                "Evidence field '{name}' of record type '{record_type}' came back \
                as {}, not a quotation.",
                json_type_name(value),
            )));
        };
        match locate_span(quotation, passage_text) {
            Some(span) => located.push(span),
            None => {
                return Err(Error::EvidenceNotFound {
                    field: name.clone(),
                    passage_id: passage_id.to_owned(),
                    // The `Display` impl truncates to the first 100
                    // characters, matching Python's `span_text[:100]` on str.
                    span_text: quotation.to_owned(),
                });
            }
        }
    }

    if located.is_empty() {
        return Err(Error::Validation(format!(
            "Record of type '{record_type}' from passage {passage_id} quoted nothing; \
            one of {} is required.",
            declared.join(", "),
        )));
    }
    Ok(located[0])
}

/// Python `repr()` for the values that can appear where a record type name is
/// expected. Only the string case is pinned (single quotes, as `{rt!r}`
/// renders them); anything else is a malformed record, rendered readably.
fn py_repr(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_owned(),
        Some(Value::String(text)) => format!("'{text}'"),
        Some(Value::Bool(true)) => "True".to_owned(),
        Some(Value::Bool(false)) => "False".to_owned(),
        Some(other) => other.to_string(),
    }
}

/// The Python `type(value).__name__` for a JSON value that arrived where a
/// quotation was declared.
///
/// Proof the final `else` fires only for objects: the caller (`anchor`) skips
/// nulls and returns strings before calling, so only booleans, numbers,
/// arrays, and objects arrive here — each named above. (The old `Null |
/// String` arm called a helper no public input could reach; matching it away
/// is removed, not waived.)
fn json_type_name(value: &Value) -> &'static str {
    debug_assert!(
        value.is_boolean() || value.is_number() || value.is_array() || value.is_object(),
        "null and str are filtered before json_type_name"
    );
    if value.is_boolean() {
        "bool"
    } else if value.as_i64().is_some() || value.as_u64().is_some() {
        "int"
    } else if value.is_number() {
        "float"
    } else if value.is_array() {
        "list"
    } else {
        "dict"
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    const PASSAGE: &str = "Once upon a time, the quick brown fox jumped over the lazy dog.";

    fn record_types() -> Map<String, Value> {
        json!({
            "claim": {
                "id": "claim",
                "fields": {
                    "assertion": {"type": "string", "required": true},
                    "quote": {"type": "evidence_span", "required": true},
                },
            },
        })
        .as_object()
        .cloned()
        .expect("test types are an object")
    }

    fn record(fields: Value) -> Value {
        json!({"record_type": "claim", "fields": fields})
    }
    /// Slice by character offsets — the contract `locate_span` returns.
    fn chars_slice(text: &str, start: usize, end: usize) -> String {
        text.chars().skip(start).take(end - start).collect()
    }

    #[test]
    fn normalize_whitespace_collapses_spaces() {
        assert_eq!(normalize_whitespace("hello   world"), "hello world");
    }

    #[test]
    fn normalize_whitespace_collapses_newlines() {
        assert_eq!(normalize_whitespace("hello\n\nworld"), "hello world");
    }

    #[test]
    fn normalize_whitespace_strips() {
        assert_eq!(normalize_whitespace("  hello  "), "hello");
    }

    #[test]
    fn normalize_whitespace_tabs() {
        assert_eq!(normalize_whitespace("hello\t\tworld"), "hello world");
    }

    #[test]
    fn locate_span_offsets_index_the_original_text() {
        let (start, end) = locate_span("quick brown fox", PASSAGE).expect("found");
        assert_eq!(&PASSAGE[start..end], "quick brown fox");
    }

    #[test]
    fn locate_span_whitespace_differences_still_index_the_original() {
        // The tolerance is in the pattern, never in the text being searched:
        // offsets address the passage exactly as stored.
        let passage = "Once upon a time, the quick\n   brown  fox jumped over.";
        let (start, end) = locate_span("the quick brown fox", passage).expect("found");
        assert_eq!(&passage[start..end], "the quick\n   brown  fox");
        assert_eq!(
            normalize_whitespace(&passage[start..end]),
            "the quick brown fox"
        );
    }

    #[test]
    fn locate_span_pathological_input_returns_none_like_python() {
        // `Regex::new` fails past its compiled-size limit (`CompiledTooBig`);
        // the `?` answers `None`, which is what CPython's own search answers
        // on the same input (probed: a 20M-char quote against a short passage
        // matches nothing). Fires the fallible-construction arm with agreed
        // behavior rather than deleting it.
        let giant = "a".repeat(20_000_000);
        assert_eq!(locate_span(&giant, "short passage"), None);
    }

    #[test]
    fn locate_span_not_found() {
        assert_eq!(locate_span("completely different text", PASSAGE), None);
    }

    #[test]
    fn locate_span_empty() {
        assert_eq!(locate_span("   ", PASSAGE), None);
        assert_eq!(locate_span("", PASSAGE), None);
    }

    #[test]
    fn locate_span_unicode_returns_char_offsets_not_byte_offsets() {
        // `café` ends at byte 12 but char 11; Python reports chars.
        let passage = "I love café society";
        let (start, end) = locate_span("café", passage).expect("found");
        assert_eq!((start, end), (7, 11));
        assert_eq!(chars_slice(passage, start, end), "café");
    }

    #[test]
    fn locate_span_char_offsets_past_non_ascii() {
        // Byte offsets would read (19, 24); CPython `str.find` reads (18, 23).
        let passage = "café au lait, the quick fox";
        let (start, end) = locate_span("quick", passage).expect("found");
        assert_eq!((start, end), (18, 23));
        assert_eq!(chars_slice(passage, start, end), "quick");
    }

    #[test]
    fn locate_span_fuzzy_char_offsets_past_non_ascii() {
        // Whitespace-tolerant path must convert too: match.start()/end() in
        // CPython are character offsets.
        let passage = "café\nquick   brown fox";
        let (start, end) = locate_span("quick brown", passage).expect("found");
        assert_eq!((start, end), (5, 18));
        assert_eq!(chars_slice(passage, start, end), "quick   brown");
    }

    #[test]
    fn validate_records_anchors_the_evidence() {
        let records = vec![record(
            json!({"assertion": "foxes jump", "quote": "the quick brown fox"}),
        )];
        let [result] = validate_records(&records, PASSAGE, "pid-1", &record_types())
            .expect("valid")
            .try_into()
            .expect("one record");
        assert_eq!(result.record_type, "claim");
        assert_eq!(
            &PASSAGE[result.evidence_start..result.evidence_end],
            "the quick brown fox"
        );
    }

    #[test]
    fn validate_records_invented_evidence_is_rejected() {
        let records = vec![record(
            json!({"assertion": "x", "quote": "a sentence never written"}),
        )];
        let err = validate_records(&records, PASSAGE, "pid-1", &record_types()).expect_err("fails");
        assert_eq!(
            std::mem::discriminant(&err),
            std::mem::discriminant(&Error::EvidenceNotFound {
                field: String::new(),
                passage_id: String::new(),
                span_text: String::new()
            })
        );
        assert_eq!(
            err.to_string(),
            "Evidence span for field 'quote' not found in passage pid-1: 'a sentence never written'"
        );
    }

    #[test]
    fn validate_records_evidence_not_found_truncates_to_100_chars() {
        let long = format!("{} and then some", "z".repeat(150));
        let records = vec![record(json!({"assertion": "x", "quote": long}))];
        let err = validate_records(&records, PASSAGE, "pid-1", &record_types()).expect_err("fails");
        assert_eq!(
            err.to_string(),
            format!(
                "Evidence span for field 'quote' not found in passage pid-1: '{}'",
                "z".repeat(100)
            )
        );
    }

    #[test]
    fn validate_records_unknown_record_type_names_declared_sorted() {
        let records = vec![json!({"record_type": "invented", "fields": {}})];
        let err = validate_records(&records, PASSAGE, "pid-1", &record_types()).expect_err("fails");
        assert_eq!(
            err.to_string(),
            "Record 0 from passage pid-1 has record_type 'invented'; the schema declares claim."
        );

        let empty = Map::new();
        let err = validate_records(&records, PASSAGE, "pid-1", &empty).expect_err("fails");
        assert_eq!(
            err.to_string(),
            "Record 0 from passage pid-1 has record_type 'invented'; the schema declares none."
        );
    }

    #[test]
    fn validate_records_missing_required_field_is_rejected() {
        let records = vec![record(json!({"quote": "the quick brown fox"}))];
        let err = validate_records(&records, PASSAGE, "pid-1", &record_types()).expect_err("fails");
        assert_eq!(
            err.to_string(),
            "Record 0 of type 'claim' from passage pid-1 is missing required field(s): assertion."
        );
    }

    #[test]
    fn validate_records_falsy_required_values_are_missing() {
        // `not fields.get(name)`: null, false, zero, `""`, and empty
        // containers are all missing, not just absent keys.
        for falsy in [
            json!(null),
            json!(false),
            json!(0),
            json!(0.0),
            json!(""),
            json!([]),
            json!({}),
        ] {
            let records = vec![record(
                json!({"assertion": falsy, "quote": "the quick brown fox"}),
            )];
            let err =
                validate_records(&records, PASSAGE, "pid-1", &record_types()).expect_err("fails");
            assert!(
                err.to_string().contains("assertion"),
                "falsy {falsy} must read as missing: {err}"
            );
        }
    }

    #[test]
    fn validate_records_record_that_quotes_nothing_is_rejected() {
        // The failure the old validator could not see: it looked for fields
        // whose *name* contained "evidence", so a quotation named `quote` was
        // never checked. Evidence fields are found by declared type now.
        let types = schema_def_without_required();
        let records = vec![record(json!({"assertion": "unsupported"}))];
        let err = validate_records(&records, PASSAGE, "pid-1", &types).expect_err("fails");
        assert_eq!(
            err.to_string(),
            "Record of type 'claim' from passage pid-1 quoted nothing; one of quote is required."
        );
    }

    #[test]
    fn validate_records_type_with_no_evidence_field_is_rejected() {
        let types = json!({
            "claim": {"id": "claim", "fields": {"assertion": {"type": "string"}}},
        })
        .as_object()
        .cloned()
        .expect("object");
        let records = vec![record(json!({"assertion": "anything"}))];
        let err = validate_records(&records, PASSAGE, "pid-1", &types).expect_err("fails");
        assert_eq!(
            err.to_string(),
            "Record type 'claim' declares no 'evidence_span' field, so nothing it produces can be checked against the corpus."
        );
    }

    #[test]
    fn validate_records_fields_must_be_an_object() {
        let records = vec![json!({"record_type": "claim", "fields": "not an object"})];
        let err = validate_records(&records, PASSAGE, "pid-1", &record_types()).expect_err("fails");
        assert_eq!(
            err.to_string(),
            "Record 0 from passage pid-1 has no 'fields' object."
        );
    }

    #[test]
    fn validate_records_non_string_evidence_names_its_json_type() {
        for (value, type_name) in [
            (json!(true), "bool"),
            (json!(3), "int"),
            (json!(3.5), "float"),
            (json!(["x"]), "list"),
            (json!({"x": 1}), "dict"),
        ] {
            let records = vec![record(json!({"assertion": "x", "quote": value}))];
            let err =
                validate_records(&records, PASSAGE, "pid-1", &record_types()).expect_err("fails");
            assert_eq!(
                err.to_string(),
                format!(
                    "Evidence field 'quote' of record type 'claim' came back as {type_name}, not a quotation."
                )
            );
        }
    }

    #[test]
    fn validate_records_null_evidence_reads_as_quoting_nothing() {
        let types = schema_def_without_required();
        let records = vec![record(json!({"assertion": "x", "quote": null}))];
        let err = validate_records(&records, PASSAGE, "pid-1", &types).expect_err("fails");
        assert!(
            err.to_string().contains("quoted nothing"),
            "null evidence is skipped, not a type error: {err}"
        );
    }

    #[test]
    fn validate_records_empty_records() {
        assert_eq!(
            validate_records(&[], PASSAGE, "pid-1", &record_types()).expect("empty"),
            vec![]
        );
    }

    #[test]
    fn validate_records_malformed_record_type_names_its_repr() {
        // Python `{record_type!r}`: a missing or null type reads `None`,
        // booleans read `True`/`False`, anything else renders readably.
        for (entry, repr) in [
            (json!({"fields": {}}), "None"),
            (json!({"record_type": null, "fields": {}}), "None"),
            (json!({"record_type": true, "fields": {}}), "True"),
            (json!({"record_type": false, "fields": {}}), "False"),
            (json!({"record_type": 3, "fields": {}}), "3"),
        ] {
            let err = validate_records(&[entry], PASSAGE, "pid-1", &record_types())
                .expect_err("malformed type fails");
            assert_eq!(
                err.to_string(),
                format!(
                    "Record 0 from passage pid-1 has record_type {repr}; the schema declares claim."
                )
            );
        }
    }

    fn schema_def_without_required() -> Map<String, Value> {
        json!({
            "claim": {
                "id": "claim",
                "fields": {
                    "assertion": {"type": "string"},
                    "quote": {"type": "evidence_span"},
                },
            },
        })
        .as_object()
        .cloned()
        .expect("object")
    }
}
