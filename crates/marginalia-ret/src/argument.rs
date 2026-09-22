//! Claim-edge validation and anchor-context window slicing.
//!
//! Python sources: `research_engine/services/argument/claims.py` (the
//! `_validate_edges` pure check plus the `ClaimWriteRefused` code / message /
//! detail contract; the atomic DB write path stays out),
//! `research_engine/services/argument/context.py` (window-slicing math) and
//! `research_engine/services/argument/rules.py` (audit ref normalisation).
//!
//! No database execution here: these are SQL-free pure decision functions.
//! The later `repos` pass executes the lookups and maps failures back onto
//! these refusal shapes.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// claims.py: ClaimWriteRefused + _validate_edges
// ---------------------------------------------------------------------------

/// Expected refusal: bad evidence or an edge to no stable claim ref.
///
/// Mirrors `ClaimWriteRefused(ValueError)`: `str()` is the message, while
/// `code` and `detail` travel alongside for machine handling.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClaimWriteRefused {
    pub code: String,
    pub message: String,
    #[serde(default)]
    pub detail: HashMap<String, serde_json::Value>,
}

impl ClaimWriteRefused {
    pub fn new(
        code: impl Into<String>,
        message: impl Into<String>,
        detail: HashMap<String, serde_json::Value>,
    ) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            detail,
        }
    }
}

impl std::fmt::Display for ClaimWriteRefused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for ClaimWriteRefused {}

/// One claim-to-claim edge draft. Only `target_ref` and `relation` feed the
/// pure validation; confidence/notes ride along for the later write pass.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClaimEdgeDraft {
    pub target_ref: String,
    pub relation: String,
}

/// Pure edge check, byte-identical to `ClaimService._validate_edges`:
/// duplicate (target, relation) pairs and self-edges refuse; anything else —
/// including an empty edge list — passes. Target *existence* needs the
/// database; use [`missing_target_refusal`] for that refusal shape.
pub fn validate_claim_edges(
    own_ref: &str,
    edges: &[ClaimEdgeDraft],
) -> Result<(), ClaimWriteRefused> {
    let mut seen: HashSet<(&str, &str)> = HashSet::new();
    for (index, edge) in edges.iter().enumerate() {
        let key = (edge.target_ref.as_str(), edge.relation.as_str());
        if !seen.insert(key) {
            return Err(ClaimWriteRefused::new(
                "invalid_input",
                format!("Edge {index} duplicates an earlier target and relation."),
                HashMap::from([("edge_index".to_string(), serde_json::json!(index))]),
            ));
        }
        if edge.target_ref == own_ref {
            return Err(ClaimWriteRefused::new(
                "invalid_input",
                // Python `{ref!r}` renders single quotes around the str.
                format!("Edge {index} points claim '{own_ref}' at itself."),
                HashMap::from([("edge_index".to_string(), serde_json::json!(index))]),
            ));
        }
    }
    Ok(())
}

/// Refusal shape for the target-existence branch of `ClaimService.upsert`
/// (`code: "not_found"`); the lookup itself stays in the `repos` pass, which
/// calls this to build the refusal it returns.
pub fn missing_target_refusal(target_ref: &str) -> ClaimWriteRefused {
    ClaimWriteRefused::new(
        "not_found",
        // Python: f"Target claim {edge.target_ref!r} does not exist."
        format!("Target claim '{target_ref}' does not exist."),
        HashMap::from([(
            "target_ref".to_string(),
            serde_json::Value::String(target_ref.to_string()),
        )]),
    )
}

// ---------------------------------------------------------------------------
// context.py: window slicing
// ---------------------------------------------------------------------------

/// Character window around a cited span within its document text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnchorWindow {
    pub window_start: usize,
    pub window_end: usize,
    pub quote_offset_in_window: usize,
    pub quote_length: usize,
}

/// Validate a raw window size from the untyped boundary.
///
/// Python guards `isinstance(window, bool) or window < 0` with
/// `ValueError("window must be a non-negative integer")`. The `u32` parameter
/// of [`slice_window`] makes a negative window unrepresentable (and a JSON
/// bool never deserialises into it), so this constructor is where that
/// refusal lives — mapped onto [`crate::Error::InvalidQuery`], which the
/// `repos` pass raises at the service boundary.
pub fn check_window(window: i64) -> crate::Result<u32> {
    u32::try_from(window).map_err(|_| {
        crate::Error::InvalidQuery("window must be a non-negative integer".to_string())
    })
}

/// Slice `[window_start, window_end)` around `[char_start, char_end)`.
///
/// Mirrors `AnchorContextService.many_for_coordinates`: the request span is
/// `[max(0, char_start - window), char_end + window)`, clamped to the
/// document (`window_end = window_start + len(text)` where the fetched text
/// is the request span cut at the document edge, i.e.
/// `min(text_len, char_end + window)`).
///
/// Span errors mirror the Python `ValueError`s byte-identically, mapped onto
/// [`crate::Error::InvalidQuery`].
pub fn slice_window(
    text_len: usize,
    char_start: i64,
    char_end: i64,
    window: u32,
) -> crate::Result<AnchorWindow> {
    if char_start < 0 || char_end <= char_start {
        return Err(crate::Error::InvalidQuery(format!(
            "Span [{char_start}, {char_end}) is not a valid address."
        )));
    }
    let start = char_start as usize;
    let end = char_end as usize;
    let window_start = start.saturating_sub(window as usize);
    let window_end = text_len.min(end.saturating_add(window as usize));
    Ok(AnchorWindow {
        window_start,
        window_end,
        quote_offset_in_window: start - window_start,
        quote_length: end - start,
    })
}

// ---------------------------------------------------------------------------
// rules.py: audit ref normalisation
// ---------------------------------------------------------------------------

/// Message for refs that are empty after stripping, verbatim from
/// `ClaimAuditService.audit` (`ValueError` there, [`crate::Error::InvalidQuery`]
/// at this boundary).
pub const EMPTY_REF_MESSAGE: &str = "refs must contain non-empty claim refs";

/// Normalise audit subjects: strip, refuse empties, dedupe preserving order.
///
/// Mirrors the `refs` prelude of `ClaimAuditService.audit`. (Python also
/// rejects non-`str` entries; every `&str` here is a string by construction,
/// so only the empty-after-strip refusal is reachable.)
pub fn normalize_audit_refs(refs: &[&str]) -> crate::Result<Vec<String>> {
    let mut seen: HashSet<&str> = HashSet::new();
    let mut checked: Vec<String> = Vec::with_capacity(refs.len());
    for candidate in refs {
        let normalized = candidate.trim();
        if normalized.is_empty() {
            return Err(crate::Error::InvalidQuery(EMPTY_REF_MESSAGE.to_string()));
        }
        if seen.insert(normalized) {
            checked.push(normalized.to_string());
        }
    }
    Ok(checked)
}

/// First checked ref absent from the known set, mirroring the
/// `NotFoundError("claim", missing[0])` branch. The `repos` pass maps the
/// return onto [`crate::Error::NotFound`].
pub fn first_missing_ref(checked: &[String], existing: &HashSet<String>) -> Option<String> {
    checked
        .iter()
        .find(|candidate| !existing.contains(*candidate))
        .cloned()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn edge(target: &str, relation: &str) -> ClaimEdgeDraft {
        ClaimEdgeDraft {
            target_ref: target.to_string(),
            relation: relation.to_string(),
        }
    }

    // -- validate_claim_edges -------------------------------------------------

    #[test]
    fn empty_edges_validate() {
        assert!(validate_claim_edges("A", &[]).is_ok());
    }

    #[test]
    fn distinct_edges_validate() {
        let edges = vec![
            edge("B", "supports"),
            edge("C", "supports"),
            edge("B", "rebuts"),
        ];
        assert!(validate_claim_edges("A", &edges).is_ok());
    }

    #[test]
    fn duplicate_target_and_relation_refuses() {
        let edges = vec![edge("B", "supports"), edge("B", "supports")];
        let err = validate_claim_edges("A", &edges).unwrap_err();
        assert_eq!(err.code, "invalid_input");
        assert_eq!(
            err.message,
            "Edge 1 duplicates an earlier target and relation."
        );
        assert_eq!(err.detail["edge_index"], serde_json::json!(1));
        assert_eq!(err.to_string(), err.message);
    }

    #[test]
    fn self_edge_refuses_with_repr_quotes() {
        let edges = vec![edge("A", "supports")];
        let err = validate_claim_edges("A", &edges).unwrap_err();
        assert_eq!(err.code, "invalid_input");
        assert_eq!(err.message, "Edge 0 points claim 'A' at itself.");
        assert_eq!(err.detail["edge_index"], serde_json::json!(0));
    }

    #[test]
    fn duplicate_wins_over_self_edge_in_order() {
        // Mirror Python's check order per edge: the duplicate test runs
        // before the self-edge test for each index.
        let edges = vec![edge("A", "supports"), edge("A", "supports")];
        let err = validate_claim_edges("A", &edges).unwrap_err();
        assert_eq!(err.message, "Edge 0 points claim 'A' at itself.");
    }

    #[test]
    fn missing_target_refusal_shape() {
        let err = missing_target_refusal("ghost");
        assert_eq!(err.code, "not_found");
        assert_eq!(err.message, "Target claim 'ghost' does not exist.");
        assert_eq!(
            err.detail["target_ref"],
            serde_json::Value::String("ghost".to_string())
        );
    }

    // -- slice_window ------------------------------------------------------------

    #[test]
    fn window_interior_span() {
        let window = slice_window(10_000, 5_000, 5_100, 1_200).unwrap();
        assert_eq!(
            window,
            AnchorWindow {
                window_start: 3_800,
                window_end: 6_300,
                quote_offset_in_window: 1_200,
                quote_length: 100,
            }
        );
    }

    #[test]
    fn window_clamps_at_text_start() {
        let window = slice_window(10_000, 100, 200, 1_200).unwrap();
        assert_eq!(window.window_start, 0);
        assert_eq!(window.window_end, 1_400);
        assert_eq!(window.quote_offset_in_window, 100);
        assert_eq!(window.quote_length, 100);
    }

    #[test]
    fn window_clamps_at_text_end() {
        let window = slice_window(10_000, 9_800, 9_900, 1_200).unwrap();
        assert_eq!(window.window_start, 8_600);
        assert_eq!(window.window_end, 10_000);
        assert_eq!(window.quote_offset_in_window, 1_200);
        assert_eq!(window.quote_length, 100);
    }

    #[test]
    fn oversized_window_covers_whole_text() {
        let window = slice_window(500, 100, 200, 1_200).unwrap();
        assert_eq!(window.window_start, 0);
        assert_eq!(window.window_end, 500);
        assert_eq!(window.quote_offset_in_window, 100);
        assert_eq!(window.quote_length, 100);
    }

    #[test]
    fn zero_window_is_just_the_span() {
        let window = slice_window(10_000, 5_000, 5_100, 0).unwrap();
        assert_eq!(window.window_start, 5_000);
        assert_eq!(window.window_end, 5_100);
        assert_eq!(window.quote_offset_in_window, 0);
        assert_eq!(window.quote_length, 100);
    }

    #[test]
    fn negative_window_refuses_as_invalid_query() {
        // Python `window < 0` (and bool) refusal, byte-identical message.
        let err = check_window(-1).unwrap_err();
        // The `to_string` below already pins the `InvalidQuery` variant via
        // its `invalid query: ` Display prefix; a separate variant assert
        // would add an uncovered false-arm for the same fact.
        assert_eq!(
            err.to_string(),
            "invalid query: window must be a non-negative integer"
        );
        assert_eq!(check_window(1_200).unwrap(), 1_200);
        assert_eq!(check_window(0).unwrap(), 0);
    }

    #[test]
    fn invalid_spans_refuse_with_address_message() {
        for (start, end) in [(-5, 10), (10, 10), (20, 10)] {
            let err = slice_window(1_000, start, end, 100).unwrap_err();
            // Variant pinned by the Display prefix in the message assert below.
            assert_eq!(
                err.to_string(),
                format!("invalid query: Span [{start}, {end}) is not a valid address.")
            );
        }
    }

    // -- rules.py ------------------------------------------------------------------

    #[test]
    fn normalize_strips_and_dedupes_preserving_order() {
        let checked = normalize_audit_refs(&[" B ", "A", "B"]).unwrap();
        assert_eq!(checked, vec!["B".to_string(), "A".to_string()]);
    }

    #[test]
    fn normalize_empty_input_is_no_subjects() {
        assert!(normalize_audit_refs(&[]).unwrap().is_empty());
    }

    #[test]
    fn normalize_empty_ref_refuses() {
        let err = normalize_audit_refs(&["A", "   "]).unwrap_err();
        // Variant pinned by the Display prefix in the message assert below.
        assert_eq!(
            err.to_string(),
            "invalid query: refs must contain non-empty claim refs"
        );
    }

    #[test]
    fn first_missing_ref_finds_first_gap() {
        let checked = vec!["A".to_string(), "B".to_string(), "C".to_string()];
        let existing: HashSet<String> = ["A".to_string(), "C".to_string()].into_iter().collect();
        assert_eq!(
            first_missing_ref(&checked, &existing),
            Some("B".to_string())
        );
        let full: HashSet<String> = checked.clone().into_iter().collect();
        assert_eq!(first_missing_ref(&checked, &full), None);
    }
}
