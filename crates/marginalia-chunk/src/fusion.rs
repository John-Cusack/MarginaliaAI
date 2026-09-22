//! Search-result fusion, mirroring `services/search/fusion.py`.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// The RRF down-weight. Kept as the module constant because the Python it
/// mirrors threads a `k` parameter it never reads — the divisor is `RRF_K`.
pub const RRF_K: f64 = 60.0;

/// One list's contribution to a fused score: 1-based rank, original score.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ListContribution {
    pub rank: i64,
    pub score: f64,
}

/// Reciprocal Rank Fusion across ranked lists.
///
/// Returns `(passage_id, rrf_score, breakdown)` sorted by score descending.
/// Ties keep first-seen order — Python's `sorted` is stable over an
/// insertion-ordered dict, and the stable sort here matches it.
///
/// Upstream threads a `k` parameter the divisor never reads; this port drops
/// it rather than carrying a dead argument for seam nostalgia.
pub fn rrf_fuse(
    ranked_lists: &[Vec<(Uuid, f64)>],
) -> Vec<(Uuid, f64, HashMap<String, ListContribution>)> {
    let mut order: Vec<Uuid> = Vec::new();
    let mut scores: HashMap<Uuid, f64> = HashMap::new();
    let mut breakdowns: HashMap<Uuid, HashMap<String, ListContribution>> = HashMap::new();

    for (list_idx, hits) in ranked_lists.iter().enumerate() {
        // One key per list, cloned per hit: `format!` parses its template on
        // every call, and this ran once per hit per list.
        let key = format!("list_{list_idx}");
        for (rank, (pid, score)) in hits.iter().enumerate() {
            if !scores.contains_key(pid) {
                order.push(*pid);
            }
            *scores.entry(*pid).or_insert(0.0) += 1.0 / (RRF_K + rank as f64 + 1.0);
            breakdowns.entry(*pid).or_default().insert(
                key.clone(),
                ListContribution {
                    rank: rank as i64 + 1,
                    score: *score,
                },
            );
        }
    }

    let mut fused: Vec<(Uuid, f64, HashMap<String, ListContribution>)> = order
        .into_iter()
        .map(|pid| {
            let breakdown = breakdowns.remove(&pid).unwrap_or_default();
            (pid, scores[&pid], breakdown)
        })
        .collect();
    // Stable: equal scores stay in first-seen order, as in Python.
    fused.sort_by(|a, b| b.1.total_cmp(&a.1));
    fused
}

/// How a hit's two normalized scores combined.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct WeightedBreakdown {
    pub vector_norm: f64,
    pub keyword_norm: f64,
}

/// Weighted-sum fusion with min-max normalization.
///
/// `alpha` weights the vector score, `1 - alpha` the keyword score. A hit in
/// one list only scores 0 on the other. Sorted by combined score descending;
/// exact ties keep vector-list order, then keyword-only order. (Python
/// iterates a `set` there — deterministic per build, but arbitrary — so ties
/// are the one place the two cannot be byte-compared; they are also the one
/// place no fixture has ever produced.)
pub fn weighted_fuse(
    vec_hits: &[(Uuid, f64)],
    kw_hits: &[(Uuid, f64)],
    alpha: f64,
) -> Vec<(Uuid, f64, WeightedBreakdown)> {
    let vec_norm = normalize(vec_hits);
    let kw_norm = normalize(kw_hits);

    let mut order: Vec<Uuid> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (pid, _) in vec_hits.iter().chain(kw_hits.iter()) {
        if seen.insert(*pid) {
            order.push(*pid);
        }
    }

    let mut results: Vec<(Uuid, f64, WeightedBreakdown)> = order
        .into_iter()
        .map(|pid| {
            let vs = vec_norm.get(&pid).copied().unwrap_or(0.0);
            let ks = kw_norm.get(&pid).copied().unwrap_or(0.0);
            let combined = alpha * vs + (1.0 - alpha) * ks;
            (
                pid,
                combined,
                WeightedBreakdown {
                    vector_norm: vs,
                    keyword_norm: ks,
                },
            )
        })
        .collect();
    results.sort_by(|a, b| b.1.total_cmp(&a.1));
    results
}

fn normalize(hits: &[(Uuid, f64)]) -> HashMap<Uuid, f64> {
    if hits.is_empty() {
        return HashMap::new();
    }
    let min_s = hits.iter().map(|(_, s)| *s).fold(f64::INFINITY, f64::min);
    let max_s = hits
        .iter()
        .map(|(_, s)| *s)
        .fold(f64::NEG_INFINITY, f64::max);
    let range = if max_s > min_s { max_s - min_s } else { 1.0 };
    hits.iter()
        .map(|(pid, s)| (*pid, (s - min_s) / range))
        .collect()
}
