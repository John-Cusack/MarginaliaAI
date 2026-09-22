//! Content hashing for frozen revisions.
//!
//! Python source: `services/works/hashing.py`. sha256 over the ordered tuple
//! of `(block_key, parent_key, position, block_type, title, body_markdown)`
//! plus sorted citation and link rows, rendered as canonical JSON: keys
//! sorted, no whitespace, UTF-8.

use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};

/// Canonical JSON: keys sorted at every level, `,`/`:` separators, UTF-8,
/// non-ASCII unescaped — `json.dumps(payload, sort_keys=True,
/// separators=(",", ":"), ensure_ascii=False)`.
///
/// Implemented as a dedicated writer (not `serde_json::to_vec`) for two
/// CPython behaviors serde does not reproduce:
/// - floats print Python-`repr` style: `1.0` keeps its `.0`, exponents carry
///   an explicit sign with at least two digits (`1e-05`, `1e+16`), and
///   non-finite values emit `NaN`/`Infinity`/`-Infinity`;
/// - strings escape exactly the set `ensure_ascii=False` escapes, with
///   lowercase `\u00xx` hex.
pub fn canonical_json(payload: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    push_value(&mut out, payload);
    out
}

fn push_value(out: &mut Vec<u8>, value: &Value) {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(number) => push_number(out, number),
        Value::String(text) => push_string(out, text),
        Value::Array(items) => {
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                push_value(out, item);
            }
            out.push(b']');
        }
        Value::Object(map) => {
            // `sort_keys=True`: order the keys explicitly. `serde_json::Map`
            // is insertion-ordered (`preserve_order`, matching Python dicts
            // everywhere else), so canonical output cannot inherit iteration.
            let mut ordered: Vec<(&String, &Value)> = map.iter().collect();
            ordered.sort_by(|left, right| left.0.cmp(right.0));
            out.push(b'{');
            for (index, (key, item)) in ordered.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                push_string(out, key);
                out.push(b':');
                push_value(out, item);
            }
            out.push(b'}');
        }
    }
}

fn push_number(out: &mut Vec<u8>, number: &Number) {
    if let Some(unsigned) = number.as_u64() {
        out.extend_from_slice(unsigned.to_string().as_bytes());
    } else if let Some(signed) = number.as_i64() {
        out.extend_from_slice(signed.to_string().as_bytes());
    } else {
        // Without `arbitrary_precision` every `Number` is u64/i64/f64, and
        // the two arms above catch both integer spellings (`as_u64` fails
        // only for negatives, `as_i64` only for values past `i64::MAX`, and
        // `as_f64` succeeds for all three spellings), so a number arriving
        // here is always a float.
        push_py_float(
            out,
            number.as_f64().expect("non-integer JSON numbers are f64"),
        );
    }
}

/// Python `repr` of a finite float, and `NaN`/`Infinity`/`-Infinity` beyond.
///
/// Ryu shortest-round-trip digits match CPython's dtoa digits, but Ryu's
/// notation choice does not match CPython's (`0.00001` vs `1e-05`), so the
/// digits are decomposed and re-laid-out by CPython's `format_float_short`
/// rule for `repr`: scientific when `decpt <= -4 || decpt > 16` (with an
/// explicit sign and at least two exponent digits), fixed otherwise, always
/// keeping a decimal point (`1.0`, never `1`).
fn push_py_float(out: &mut Vec<u8>, float: f64) {
    if float.is_nan() {
        out.extend_from_slice(b"NaN");
        return;
    }
    if float.is_infinite() {
        out.extend_from_slice(if float > 0.0 {
            b"Infinity"
        } else {
            b"-Infinity"
        });
        return;
    }
    let text = ryu::Buffer::new().format(float).to_owned();
    let body = text.strip_prefix('-').unwrap_or(&text);
    let negative = body.len() != text.len();
    let (mantissa, exp) = match body.find('e') {
        // Ryu exponents are small decimal integers by construction.
        Some(at) => (
            &body[..at],
            body[at + 1..]
                .parse::<i32>()
                .expect("ryu exponent parses as i32"),
        ),
        None => (body, 0),
    };
    let (int_part, frac_part) = match mantissa.find('.') {
        Some(at) => (&mantissa[..at], &mantissa[at + 1..]),
        None => (mantissa, ""),
    };
    // `decpt` in the David Gay convention: value = 0.digits × 10^decpt.
    let raw_digits = format!("{int_part}{frac_part}");
    let mut decpt = int_part.len() as i32 + exp;
    let unpadded = raw_digits.trim_start_matches('0');
    decpt -= (raw_digits.len() - unpadded.len()) as i32;
    let digits = unpadded.trim_end_matches('0');
    if digits.is_empty() {
        // Only ±0.0 has no nonzero digit.
        out.extend_from_slice(if negative { b"-0.0" } else { b"0.0" });
        return;
    }
    if negative {
        out.push(b'-');
    }
    let count = digits.len() as i32;
    if decpt <= -4 || decpt > 16 {
        let bytes = digits.as_bytes();
        out.push(bytes[0]);
        if bytes.len() > 1 {
            out.push(b'.');
            out.extend_from_slice(&bytes[1..]);
        }
        let exponent = decpt - 1;
        out.push(b'e');
        out.push(if exponent < 0 { b'-' } else { b'+' });
        let magnitude = exponent.unsigned_abs().to_string();
        if magnitude.len() < 2 {
            out.push(b'0');
        }
        out.extend_from_slice(magnitude.as_bytes());
    } else if decpt <= 0 {
        out.extend_from_slice(b"0.");
        for _ in 0..-decpt {
            out.push(b'0');
        }
        out.extend_from_slice(digits.as_bytes());
    } else if decpt >= count {
        out.extend_from_slice(digits.as_bytes());
        for _ in 0..(decpt - count) {
            out.push(b'0');
        }
        out.extend_from_slice(b".0");
    } else {
        let split = decpt as usize;
        let bytes = digits.as_bytes();
        out.extend_from_slice(&bytes[..split]);
        out.push(b'.');
        out.extend_from_slice(&bytes[split..]);
    }
}

fn push_string(out: &mut Vec<u8>, text: &str) {
    out.push(b'"');
    for ch in text.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{08}' => out.extend_from_slice(b"\\b"),
            '\u{0C}' => out.extend_from_slice(b"\\f"),
            ch if (ch as u32) < 0x20 => {
                out.extend_from_slice(format!("\\u{:04x}", ch as u32).as_bytes());
            }
            ch => {
                let mut encoded = [0u8; 4];
                out.extend_from_slice(ch.encode_utf8(&mut encoded).as_bytes());
            }
        }
    }
    out.push(b'"');
}

/// Hash authored content. Callers pass rows already in a stable order.
///
/// Blocks arrive in tree order; citations and links are sorted here by
/// everything that identifies them except volatile columns.
pub fn compute_content_hash(
    blocks: &[Map<String, Value>],
    citations: &[Map<String, Value>],
    source_links: &[Map<String, Value>],
    entity_links: &[Map<String, Value>],
) -> [u8; 32] {
    let payload = serde_json::json!({
        "blocks": blocks.iter().map(|block| serde_json::json!({
            "block_key": block.get("block_key"),
            "parent_key": block.get("parent_key"),
            "position": block.get("position"),
            "block_type": block.get("block_type"),
            "title": block.get("title"),
            "body_markdown": block.get("body_markdown"),
        })).collect::<Vec<_>>(),
        "citations": sorted_rows(
            citations,
            &[
                "block_key",
                "citation_key",
                "position",
                "intent",
                "placement",
                "edition_id",
                "edition_key",
                "source_span_id",
                "quoted_text",
                "verify_status",
                "locator",
                "prefix",
                "suffix",
                "suppress_author",
            ],
            &["block_key", "citation_key", "position"],
        ),
        "source_links": sorted_rows(
            source_links,
            &[
                "block_key",
                "source_span_id",
                "relation",
                "confidence",
                "note",
            ],
            &["block_key", "source_span_id", "relation"],
        ),
        "entity_links": sorted_rows(
            entity_links,
            &["block_key", "entity_id", "relation", "surface_form"],
            &["block_key", "entity_id", "relation"],
        ),
    });
    let digest = Sha256::digest(canonical_json(&payload));
    digest.into()
}

fn sorted_rows(rows: &[Map<String, Value>], columns: &[&str], key: &[&str]) -> Vec<Value> {
    let mut projected: Vec<Map<String, Value>> = rows
        .iter()
        .map(|row| {
            columns
                .iter()
                .map(|column| ((*column).to_owned(), row.get(*column).cloned().into()))
                .collect()
        })
        .collect();
    projected.sort_by(|a, b| {
        key.iter()
            .map(|column| compare_json(&a[*column], &b[*column]))
            .find(|order| !order.is_eq())
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    projected.into_iter().map(Value::Object).collect()
}

fn compare_numbers(a: &Number, b: &Number) -> std::cmp::Ordering {
    // Sort keys are homogeneous per column (positions are ints, keys are
    // strings), so equal kinds meet here; cross-kind pairs from malformed
    // caller input order by value.
    if let (Some(x), Some(y)) = (a.as_i64(), b.as_i64()) {
        return x.cmp(&y);
    }
    if let (Some(x), Some(y)) = (a.as_u64(), b.as_u64()) {
        return x.cmp(&y);
    }
    let x = a
        .as_f64()
        .expect("Number without arbitrary_precision is f64-able");
    let y = b
        .as_f64()
        .expect("Number without arbitrary_precision is f64-able");
    x.partial_cmp(&y).unwrap_or(std::cmp::Ordering::Equal)
}

/// Total order over the JSON scalars the sort keys hold (`str`, `int`,
/// `None`, `bool`, `float`): `None` sorts first like Python, then bools
/// (as `False < True`), numbers (by value, floats and ints compared
/// together exactly as Python compares them), then strings by code point.
/// Mixed incomparable kinds fall back to a fixed kind rank so the sort is
/// total — the payloads only ever hold one kind per column.
fn compare_json(a: &Value, b: &Value) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    fn rank(value: &Value) -> u8 {
        match value {
            Value::Null => 0,
            Value::Bool(_) => 1,
            Value::Number(_) => 2,
            Value::String(_) => 3,
            Value::Array(_) => 4,
            Value::Object(_) => 5,
        }
    }
    if rank(a) != rank(b) {
        return rank(a).cmp(&rank(b));
    }
    match (a, b) {
        (Value::Null, Value::Null) => Ordering::Equal,
        (Value::Bool(x), Value::Bool(y)) => x.cmp(y),
        (Value::Number(x), Value::Number(y)) => compare_numbers(x, y),
        (Value::String(x), Value::String(y)) => x.cmp(y),
        _ => Ordering::Equal,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn canonical_str(payload: &Value) -> String {
        String::from_utf8(canonical_json(payload)).expect("canonical JSON is UTF-8")
    }

    fn hex32(digest: [u8; 32]) -> String {
        digest.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    fn row(pairs: &[(&str, Value)]) -> Map<String, Value> {
        pairs
            .iter()
            .map(|(key, value)| ((*key).to_owned(), value.clone()))
            .collect()
    }

    #[test]
    fn canonical_sorts_keys_and_compacts() {
        // `json.dumps(payload, sort_keys=True, separators=(",", ":"))`.
        let payload = json!({"z": 1, "a": {"d": 4, "b": [3, 2, {"y": 1, "x": 2}]}, "m": null});
        assert_eq!(
            canonical_str(&payload),
            r#"{"a":{"b":[3,2,{"x":2,"y":1}],"d":4},"m":null,"z":1}"#
        );
        assert_eq!(canonical_str(&json!({})), "{}");
        assert_eq!(canonical_str(&json!([])), "[]");
        assert_eq!(
            canonical_str(&json!({"t": true, "f": false, "n": null})),
            r#"{"f":false,"n":null,"t":true}"#
        );
    }

    #[test]
    fn canonical_keeps_non_ascii_unescaped() {
        // `ensure_ascii=False`: BMP and astral text rides through as UTF-8.
        let payload = json!({"k": "héllo→日本語😀é", "é": "v"});
        assert_eq!(
            canonical_str(&payload),
            r#"{"k":"héllo→日本語😀é","é":"v"}"#
        );
    }

    #[test]
    fn canonical_escapes_controls_python_style() {
        // CPython `json.dumps(..., ensure_ascii=False)` on
        // "\x00\x08\x0c\x1f\x7f  ":
        // named escapes for \b \f, lowercase \u00xx for the rest, while
        // 0x7f/U+2028/U+2029 pass through raw.
        let raw: String = [0x00u32, 0x08, 0x0c, 0x1f, 0x7f, 0x2028, 0x2029]
            .into_iter()
            .map(|code| char::from_u32(code).expect("valid test scalar"))
            .collect();
        let payload = json!({"s": raw});
        let mut expected = String::from("{\"s\":\"\\u0000\\b\\f\\u001f");
        for code in [0x7fu32, 0x2028, 0x2029] {
            expected.push(char::from_u32(code).expect("valid test scalar"));
        }
        expected.push_str("\"}");
        assert_eq!(canonical_str(&payload), expected);
    }

    #[test]
    fn canonical_floats_match_python_repr() {
        // Each pair is `(rust f64) -> CPython json.dumps output`.
        let cases: &[(f64, &str)] = &[
            (1.0, "1.0"),
            (0.5, "0.5"),
            (1e-5, "1e-05"),
            (1e16, "1e+16"),
            (1e100, "1e+100"),
            (1e-7, "1e-07"),
            (2.5e-7, "2.5e-07"),
            (1.0 / 3.0, "0.3333333333333333"),
            (0.1 + 0.2, "0.30000000000000004"),
            (123456789.12345679, "123456789.12345679"),
            (-0.0, "-0.0"),
            (0.0, "0.0"),
            (5e-324, "5e-324"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
            (100.0, "100.0"),
            (1e15, "1000000000000000.0"),
            (1e21, "1e+21"),
            (1e22, "1e+22"),
            (123456.789e10, "1234567890000000.0"),
        ];
        for (float, expected) in cases {
            assert_eq!(canonical_str(&json!(*float)), *expected, "float {float:?}");
        }
        assert_eq!(canonical_str(&json!(165)), "165");
    }

    #[test]
    fn canonical_nonfinite_matches_python() {
        // `Value` cannot hold non-finite floats (invalid JSON), so the
        // writer unit is exercised directly: CPython emits bare
        // `NaN`/`Infinity`/`-Infinity` tokens.
        for (float, expected) in [
            (f64::NAN, "NaN"),
            (f64::INFINITY, "Infinity"),
            (f64::NEG_INFINITY, "-Infinity"),
        ] {
            let mut out = Vec::new();
            push_py_float(&mut out, float);
            assert_eq!(out, expected.as_bytes());
        }
    }

    #[test]
    fn canonical_int_boundaries_exact() {
        assert_eq!(canonical_str(&json!(u64::MAX)), "18446744073709551615");
        assert_eq!(canonical_str(&json!(i64::MIN)), "-9223372036854775808");
    }

    /// One table of canonical-JSON rows: `(blocks, citations, source_links,
    /// entity_links)` share the shape, so the tuple names it once.
    type JsonRows = Vec<Map<String, Value>>;

    fn crafted_rows() -> (JsonRows, JsonRows, JsonRows, JsonRows) {
        let blocks = vec![
            row(&[
                ("block_key", json!("b2")),
                ("parent_key", Value::Null),
                ("position", json!(1)),
                ("block_type", json!("p")),
                ("title", json!("ünï→😀")),
                ("body_markdown", json!("hello\0world")),
            ]),
            row(&[
                ("block_key", json!("b1")),
                ("parent_key", json!("root")),
                ("position", json!(0)),
                ("block_type", json!("h")),
                ("title", json!("")),
                ("body_markdown", json!("x")),
            ]),
        ];
        let citation = |key: &str, position: i64, suppress: bool| {
            row(&[
                ("block_key", json!("b1")),
                ("citation_key", json!(key)),
                ("position", json!(position)),
                ("intent", json!("q")),
                ("placement", json!("i")),
                ("edition_id", json!("e")),
                ("edition_key", json!("ek")),
                ("source_span_id", json!("s")),
                ("quoted_text", json!("qt")),
                ("verify_status", json!("v")),
                ("locator", json!("l")),
                ("prefix", json!("p")),
                ("suffix", json!("s")),
                ("suppress_author", json!(suppress)),
            ])
        };
        let citations = vec![citation("k2", 1, false), citation("k1", 0, true)];
        let source_links = vec![
            row(&[
                ("block_key", json!("b")),
                ("source_span_id", json!("s")),
                ("relation", json!("r")),
                ("confidence", json!(1e-7)),
                ("note", Value::Null),
            ]),
            row(&[
                ("block_key", json!("a")),
                ("source_span_id", json!("s")),
                ("relation", json!("r")),
                ("confidence", json!(0.9999999)),
                ("note", json!("n")),
            ]),
        ];
        let entity_links = vec![
            row(&[
                ("block_key", json!("b")),
                ("entity_id", json!("e2")),
                ("relation", json!("r")),
                ("surface_form", json!("sf")),
            ]),
            row(&[
                ("block_key", json!("a")),
                ("entity_id", json!("e1")),
                ("relation", json!("r")),
                ("surface_form", json!("sf")),
            ]),
        ];
        (blocks, citations, source_links, entity_links)
    }

    #[test]
    fn content_hash_matches_python_crafted_rows() {
        // sha256 hex from CPython `compute_content_hash` on the mirrored
        // rows (unsorted citation/link input, unicode titles, 1e-7
        // confidence, None parent/note): sorting must normalize them.
        let (blocks, citations, source_links, entity_links) = crafted_rows();
        assert_eq!(
            hex32(compute_content_hash(
                &blocks,
                &citations,
                &source_links,
                &entity_links
            )),
            "b9e1873f6d24d82d42d9e63e5319a0b014387a0495c199c9ae5a8f0474faa36c"
        );
    }

    #[test]
    fn content_hash_empty_matches_python() {
        let empty: Vec<Map<String, Value>> = Vec::new();
        assert_eq!(
            hex32(compute_content_hash(&empty, &empty, &empty, &empty)),
            "e7d73e494e20a31506f18010e0745b7b2b030c6b8c52329f0911e720f899754a"
        );
    }

    #[test]
    fn content_hash_sorts_unsorted_input() {
        // Row order within citations/links is volatile; the hash sorts it
        // away, but block tree order stays significant.
        let (blocks, citations, source_links, entity_links) = crafted_rows();
        let ordered = compute_content_hash(&blocks, &citations, &source_links, &entity_links);
        let reversed_citations: Vec<_> = citations.iter().rev().cloned().collect();
        let reversed_sources: Vec<_> = source_links.iter().rev().cloned().collect();
        let reversed_entities: Vec<_> = entity_links.iter().rev().cloned().collect();
        assert_eq!(
            compute_content_hash(
                &blocks,
                &reversed_citations,
                &reversed_sources,
                &reversed_entities
            ),
            ordered
        );
        let reversed_blocks: Vec<_> = blocks.iter().rev().cloned().collect();
        assert_ne!(
            compute_content_hash(&reversed_blocks, &citations, &source_links, &entity_links),
            ordered
        );
    }

    #[test]
    fn canonical_negative_and_small_floats_match_python() {
        // The `-` prefix and the `0.00..` fixed layout: CPython
        // `json.dumps` of the same values.
        for (float, expected) in [
            (-1.5, "-1.5"),
            (-1e-5, "-1e-05"),
            (0.01, "0.01"),
            (0.001, "0.001"),
        ] {
            assert_eq!(canonical_str(&json!(float)), expected, "float {float:?}");
        }
    }

    #[test]
    fn canonical_escapes_quote_backslash_and_whitespace() {
        // CPython `json.dumps('a\"b\\c\\nd\\re\\tf\\x00g', ensure_ascii=False)`:
        // `\"`, `\\`, `\\n`, `\\r`, `\\t` short escapes, `\\u0000` for NUL.
        assert_eq!(
            canonical_str(&json!("a\"b\\c\nd\re\tf\x00g")),
            "\"a\\\"b\\\\c\\nd\\re\\tf\\u0000g\""
        );
    }

    #[test]
    fn sort_keys_order_positions_numerically() {
        // 9 sorts before 10 numerically but after it lexicographically, so
        // the exact row order proves value (not string) ordering for ints,
        // u64s past `i64::MAX`, and mixed int/float positions — the
        // `sorted(..., key=tuple)` Python semantics.
        for positions in [
            vec![json!(10), json!(9)],
            vec![json!(u64::MAX), json!(9_999_999_999_999_999_999u64)],
            vec![json!(10.5), json!(9)],
        ] {
            let input: Vec<Map<String, Value>> = positions
                .iter()
                .map(|position| row(&[("position", position.clone())]))
                .collect();
            let sorted = sorted_rows(&input, &["position"], &["position"]);
            let ordered: Vec<Value> = sorted
                .iter()
                .map(|value| value.get("position").cloned().unwrap_or(Value::Null))
                .collect();
            let expected: Vec<Value> = positions.iter().rev().cloned().collect();
            assert_eq!(ordered, expected, "positions {positions:?}");
        }
    }

    #[test]
    fn sorted_rows_ranks_mixed_scalar_kinds() {
        // The total order over sort keys: `None` first like Python, then
        // bools (`False < True`), numbers by value, strings by code point;
        // arrays and mappings last by kind rank (payloads never mix kinds
        // per column — Python's tuple sort would raise there instead).
        let keyed = |value: Value| row(&[("k", value)]);
        let input = vec![
            keyed(json!("a")),
            keyed(json!({})),
            keyed(json!(true)),
            keyed(json!(3)),
            keyed(Value::Null),
            keyed(json!([])),
            keyed(json!(false)),
            keyed(json!(1)),
            keyed(json!("b")),
            keyed(json!([])),
            keyed(Value::Null),
        ];
        assert_eq!(
            sorted_rows(&input, &["k"], &["k"]),
            vec![
                Value::Null,
                Value::Null,
                json!(false),
                json!(true),
                json!(1),
                json!(3),
                json!("a"),
                json!("b"),
                json!([]),
                json!([]),
                json!({}),
            ]
            .into_iter()
            .map(|key| Value::Object(Map::from_iter([("k".to_owned(), key)])))
            .collect::<Vec<_>>()
        );
    }
}
