//! `scripts/discover_overlaps.py` (pure parts): overlap edge assembly.
//!
//! The hybrid search and the database writes stay Python; what ports is the
//! ranking both sides share: the score threshold, dedup-keeping-max, the
//! top-three ordering, the combined-confidence gate, and the edge drafts —
//! including the document-level edge. `--themes` dumps the eight analysis
//! themes; `--process` ranks caller-supplied hits for one theme.

use std::collections::HashMap;

use serde_json::{json, Value};

/// The eight conceptual themes the analysis searches from both sides.
fn themes() -> Value {
    json!([
        {"id": "property_rights_as_moral_foundation",
         "label": "Property Rights as a Moral Foundation",
         "queries": [
            "property rights moral foundation sacred inviolable justice",
            "taking property without authorization is theft",
            "private property sacred commutative justice"]},
        {"id": "government_coercion",
         "label": "Government Force / Coercion as the Mechanism of the State",
         "queries": [
            "government monopoly on legitimate use of force compel compliance",
            "compulsion coercion by the ruler government force",
            "forcing citizens to use money of the government's choice"]},
        {"id": "inflation_as_theft",
         "label": "Inflation / Money Creation as Theft and Redistribution",
         "queries": [
            "inflation redistributes real income illegitimate gains",
            "money production redistributes wealth from poor to rich",
            "redistribution unauthorized taking of property"]},
        {"id": "voluntariness_principle",
         "label": "The Voluntariness Principle",
         "queries": [
            "voluntary cooperation without violating property rights",
            "giving must not be reluctantly or under compulsion",
            "free responsible initiatives private individuals"]},
        {"id": "christian_moral_tradition",
         "label": "The Scholastic / Christian Moral Tradition",
         "queries": [
            "scholastic tradition Aquinas Oresme natural law moral reasoning",
            "Scripture Eighth Commandment Romans 13 biblical ethics",
            "Christian morals economics compatible Austrian"]},
        {"id": "government_as_beneficiary",
         "label": "Government as Beneficiary of the Unjust System",
         "queries": [
            "government main beneficiary inflation unjust system",
            "state third party no ownership claim inserts itself",
            "legal monopolies instruments of social injustice"]},
        {"id": "institutional_spheres_of_authority",
         "label": "Institutional Design — Proper Spheres of Authority",
         "queries": [
            "poverty relief assigned to individuals church not state",
            "government should not run banks or produce paper money",
            "institutional design coercive mechanism incompatible voluntary"]},
        {"id": "debasement_as_theft",
         "label": "Debasement as the Historical Paradigm of Government Theft",
         "queries": [
            "debasement standard form of inflation altering coins",
            "debasement inherently unjust never permissible",
            "Mosaic theocracy authorized compulsory redistribution"]}
    ])
}

/// Python's `round(x, 4)`: half to even on the exact binary value.
///
/// Scaling first and rounding the product double-rounds: `1.12345 * 10000`
/// is exactly `11234.5` in `f64`, which ties to even (`1.1234`), while the
/// exact binary value sits above the tie (`1.1235`). Rounding the correctly
/// rounded twenty-place expansion instead — which settles every tie a
/// score this size can hold — matches `round()` exactly.
fn round4(x: f64) -> f64 {
    if !x.is_finite() || x == 0.0 {
        return x;
    }
    let neg = x < 0.0;
    let rendered = format!("{:.20}", x.abs());
    let (int, frac) = rendered.split_once('.').unwrap_or((rendered.as_str(), ""));
    let mut digits: Vec<u8> = int.bytes().chain(frac.bytes().take(4)).collect();
    let mut int_len = int.len();
    let fifth = frac.as_bytes().get(4).copied().unwrap_or(b'0');
    let rest_zero = frac
        .as_bytes()
        .get(5..)
        .is_none_or(|rest| rest.iter().all(|b| *b == b'0'));
    let odd_kept = digits.last().is_some_and(|d| d % 2 == 1);
    if fifth > b'5' || (fifth == b'5' && (!rest_zero || odd_kept)) {
        // Add one unit in the last kept place, carrying left past the
        // point; a carry out front (9.9999 to 10.0000) shifts it right.
        let mut carry = true;
        for digit in digits.iter_mut().rev() {
            if !carry {
                break;
            }
            if *digit == b'9' {
                *digit = b'0';
            } else {
                *digit += 1;
                carry = false;
            }
        }
        if carry {
            digits.insert(0, b'1');
            int_len += 1;
        }
    }
    digits.truncate(int_len + 4);
    let text = format!(
        "{}{}.{}",
        if neg { "-" } else { "" },
        String::from_utf8_lossy(&digits[..int_len.min(digits.len())]),
        String::from_utf8_lossy(&digits[int_len.min(digits.len())..])
    );
    text.parse::<f64>().unwrap_or(x)
}
/// Deduplicate hits keeping the highest score per passage, then the top
/// three by score. The sort is stable, so ties keep first-seen order —
/// exactly the dict-then-sorted order upstream.
fn top_three(hits: &[(String, f64)], min_score: f64) -> Vec<(String, f64)> {
    let mut best: HashMap<&str, f64> = HashMap::new();
    let mut order: Vec<&str> = Vec::new();
    for (pid, score) in hits {
        if *score < min_score {
            continue;
        }
        match best.get(pid.as_str()) {
            Some(current) if *current >= *score => {}
            _ => {
                if !best.contains_key(pid.as_str()) {
                    order.push(pid);
                }
                best.insert(pid, *score);
            }
        }
    }
    let mut ranked: Vec<(String, f64)> = order
        .into_iter()
        .map(|pid| (pid.to_owned(), best[pid]))
        .collect();
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    ranked.truncate(3);
    ranked
}

fn process(
    theme_id: &str,
    theme_label: &str,
    article_hits: &[(String, f64)],
    book_hits: &[(String, f64)],
) -> Vec<Value> {
    let top_article = top_three(article_hits, 0.3);
    let top_book = top_three(book_hits, 0.3);
    let mut edges = Vec::new();
    for (a_pid, a_score) in &top_article {
        for (b_pid, b_score) in &top_book {
            let combined = a_score.min(*b_score);
            // Only edges where both sides show reasonable relevance.
            if combined < 0.35 {
                continue;
            }
            edges.push(json!({
                "source_kind": "passage",
                "source_id": a_pid,
                "target_kind": "passage",
                "target_id": b_pid,
                "relation_type": "conceptual_overlap",
                "attributes": {
                    "theme": theme_id,
                    "theme_label": theme_label,
                    "source_document": "article",
                    "target_document": "book",
                    "source_score": round4(*a_score),
                    "target_score": round4(*b_score),
                },
                "confidence": round4(combined),
            }));
        }
    }
    edges
}

fn read_hits(path: &str) -> Vec<(String, f64)> {
    let raw = std::fs::read_to_string(path).unwrap();
    serde_json::from_str::<Vec<Value>>(&raw)
        .unwrap()
        .iter()
        .map(|hit| {
            (
                hit[0].as_str().unwrap().to_owned(),
                hit[1].as_f64().unwrap(),
            )
        })
        .collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--themes") {
        println!("{}", themes());
        return;
    }
    if args.iter().any(|a| a == "--doc-edge") {
        let ids: Vec<Value> = themes()
            .as_array()
            .unwrap()
            .iter()
            .map(|theme| theme["id"].clone())
            .collect();
        println!(
            "{}",
            json!({
                "source_kind": "document",
                "target_kind": "document",
                "relation_type": "conceptual_overlap",
                "attributes": {
                    "themes": ids,
                    "description": "Both works argue that redistribution through government coercion is a violation of property rights regardless of good intentions, and that the proper alternative is voluntary action by individuals and private institutions.",
                    "article_framework": "biblical_protestant",
                    "book_framework": "scholastic_catholic",
                    "article_domain": "fiscal_policy",
                    "book_domain": "monetary_policy",
                },
                "confidence": 0.95,
            })
        );
        return;
    }
    let get = |name: &str| {
        args.iter()
            .position(|a| a == name)
            .and_then(|i| args.get(i + 1).cloned())
            .unwrap_or_else(|| {
                eprintln!("{name} is required");
                std::process::exit(2);
            })
    };
    let edges = process(
        &get("--theme"),
        &get("--label"),
        &read_hits(&get("--article-hits")),
        &read_hits(&get("--book-hits")),
    );
    println!("{}", Value::Array(edges));
}
