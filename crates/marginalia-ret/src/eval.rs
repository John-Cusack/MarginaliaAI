//! Retrieval-eval scoring, queryset shapes, and run comparison.
//!
//! Python sources: `research_engine/eval/metrics.py`,
//! `research_engine/eval/queryset.py`, `research_engine/eval/runner.py`.
//!
//! Pure and deterministic: no I/O, no database. The async engine execution in
//! `run_queryset` (building a container, issuing searches, closing it) stays
//! out; [`score_query`] captures the per-query scoring body 1:1 so the later
//! `repos` pass can drive it.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};
use uuid::Uuid;

// ---------------------------------------------------------------------------
// metrics.py
// ---------------------------------------------------------------------------

/// Fraction of relevant passages that appear in the top *k*.
///
/// Undefined with no relevant passages; returns 1.0, since nothing was missed.
///
/// Mirrors `metrics.recall_at_k`, including the set semantics: duplicate ids
/// in `retrieved` count once, exactly like `set(retrieved[:k]) & relevant`.
pub fn recall_at_k(retrieved: &[Uuid], relevant: &[Uuid], k: usize) -> f64 {
    let relevant_set: HashSet<Uuid> = relevant.iter().copied().collect();
    if relevant_set.is_empty() {
        return 1.0;
    }
    let seen: HashSet<Uuid> = retrieved.iter().take(k).copied().collect();
    seen.intersection(&relevant_set).count() as f64 / relevant_set.len() as f64
}

/// Fraction of the top *k* that is relevant.
///
/// Python guards `k <= 0`; `k` is `usize` here so a negative cutoff is
/// unrepresentable and the guard is `k == 0`.
pub fn precision_at_k(retrieved: &[Uuid], relevant: &[Uuid], k: usize) -> f64 {
    if k == 0 {
        return 0.0;
    }
    let relevant_set: HashSet<Uuid> = relevant.iter().copied().collect();
    let seen: HashSet<Uuid> = retrieved.iter().take(k).copied().collect();
    seen.intersection(&relevant_set).count() as f64 / k as f64
}

/// 1/rank of the first relevant result (1-based); 0.0 if none is retrieved.
///
/// Averaged over queries this is MRR.
pub fn reciprocal_rank(retrieved: &[Uuid], relevant: &[Uuid]) -> f64 {
    let relevant_set: HashSet<Uuid> = relevant.iter().copied().collect();
    for (position, passage_id) in retrieved.iter().enumerate() {
        if relevant_set.contains(passage_id) {
            return 1.0 / (position + 1) as f64;
        }
    }
    0.0
}

/// Discounted cumulative gain: `gain / log2(rank + 1)` with 1-based rank.
///
/// Plain fold summation: `dcg` feeds comparisons, never `int()` truncation,
/// so CPython's Neumaier-compensated `sum()` is not mirrored here.
pub fn dcg(gains: &[f64]) -> f64 {
    gains
        .iter()
        .enumerate()
        .map(|(index, gain)| gain / ((index + 1) as f64 + 1.0).log2())
        .sum()
}

/// Relevance judgments for [`ndcg_at_k`]: a bare set reads as all-1.0
/// (binary) or a mapping of passage id to graded score.
#[derive(Debug, Clone, Copy)]
pub enum NdcgRelevant<'a> {
    Binary(&'a [Uuid]),
    Graded(&'a [(Uuid, f64)]),
}

/// Normalised discounted cumulative gain over the top *k*.
///
/// Empty judgments score 1.0 (nothing was missed); a zero ideal (e.g. all
/// grades are 0.0) scores 0.0, mirroring `actual / ideal if ideal else 0.0`.
pub fn ndcg_at_k(retrieved: &[Uuid], relevant: NdcgRelevant<'_>, k: usize) -> f64 {
    let grades: HashMap<Uuid, f64> = match relevant {
        NdcgRelevant::Binary(ids) => ids.iter().map(|id| (*id, 1.0)).collect(),
        NdcgRelevant::Graded(pairs) => pairs.iter().map(|(id, grade)| (*id, *grade)).collect(),
    };
    if grades.is_empty() {
        return 1.0;
    }
    let actual = dcg(&retrieved
        .iter()
        .take(k)
        .map(|id| grades.get(id).copied().unwrap_or(0.0))
        .collect::<Vec<_>>());
    // `sorted(grades.values(), reverse=True)[:k]`; `total_cmp` keeps the
    // ordering deterministic even if a NaN grade ever slips through.
    let mut ideal_grades: Vec<f64> = grades.values().copied().collect();
    ideal_grades.sort_by(|a, b| b.total_cmp(a));
    ideal_grades.truncate(k);
    let ideal = dcg(&ideal_grades);
    if ideal == 0.0 {
        0.0
    } else {
        actual / ideal
    }
}

// ---------------------------------------------------------------------------
// queryset.py shapes
// ---------------------------------------------------------------------------

/// One query and the passages a human judged relevant to it.
///
/// `relevant` maps passage id to graded relevance (1.0 = on point); a bare
/// list in the YAML reads as all-1.0, which the loader (not ported here)
/// expands before deserialising into this shape.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EvalQuery {
    pub query: String,
    #[serde(default)]
    pub relevant: HashMap<Uuid, f64>,
    #[serde(default)]
    pub note: Option<String>,
    /// Filters to apply, matching `SearchFilters`.
    #[serde(default)]
    pub filters: Option<HashMap<String, serde_json::Value>>,
}

impl EvalQuery {
    /// Passage ids with a judgment, mirroring `EvalQuery.relevant_ids`.
    pub fn relevant_ids(&self) -> HashSet<Uuid> {
        self.relevant.keys().copied().collect()
    }

    /// Same ids in deterministic (sorted) order for canonical output.
    /// Iteration order is never inherited; callers needing a stable
    /// rendering sort explicitly.
    pub fn relevant_ids_sorted(&self) -> Vec<Uuid> {
        let mut ids: Vec<Uuid> = self.relevant.keys().copied().collect();
        ids.sort();
        ids
    }
}

/// A named, versioned set of judged queries.
///
/// Frozen on purpose: a regression set that drifts measures nothing. Add new
/// queries as a new version rather than editing in place.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QuerySet {
    pub name: String,
    #[serde(default = "default_queryset_version")]
    pub version: String,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub queries: Vec<EvalQuery>,
}

fn default_queryset_version() -> String {
    "1".to_string()
}

impl QuerySet {
    pub fn len(&self) -> usize {
        self.queries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.queries.is_empty()
    }
}

// ---------------------------------------------------------------------------
// runner.py shapes
// ---------------------------------------------------------------------------

/// Per-query scores for one judged query.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QueryResult {
    pub query: String,
    pub retrieved: Vec<Uuid>,
    pub recall: f64,
    pub precision: f64,
    pub reciprocal_rank: f64,
    pub ndcg: f64,
    /// Retrieval returned nothing at all — usually a filter that eliminated
    /// every candidate, which averages look identical to "ranked badly".
    #[serde(default)]
    pub empty: bool,
}

/// The per-query scoring body of `run_queryset`: score one judged query's
/// retrieved ids against its judgments at cutoff *k*.
pub fn score_query(query: &str, retrieved: Vec<Uuid>, judged: &EvalQuery, k: usize) -> QueryResult {
    let relevant_ids: Vec<Uuid> = judged.relevant_ids_sorted();
    let graded: Vec<(Uuid, f64)> = judged
        .relevant
        .iter()
        .map(|(id, grade)| (*id, *grade))
        .collect();
    let empty = retrieved.is_empty();
    let recall = recall_at_k(&retrieved, &relevant_ids, k);
    let precision = precision_at_k(&retrieved, &relevant_ids, k);
    let reciprocal_rank = reciprocal_rank(&retrieved, &relevant_ids);
    let ndcg = ndcg_at_k(&retrieved, NdcgRelevant::Graded(&graded), k);
    QueryResult {
        query: query.to_string(),
        retrieved,
        recall,
        precision,
        reciprocal_rank,
        ndcg,
        empty,
    }
}
/// Scores for one engine configuration over a whole query set.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunResult {
    pub config: String,
    pub k: usize,
    #[serde(default)]
    pub queries: Vec<QueryResult>,
}

impl RunResult {
    /// `statistics.mean` over the per-query values, 0.0 with no queries.
    ///
    /// Deviation note: Python's `statistics.mean` sums exactly (Fraction
    /// based) and then divides. This ports the summation with
    /// Neumaier-compensated accumulation, which agrees with the exact mean
    /// on ordinary score values; on adversarial cancellation-heavy inputs
    /// the two can differ by ~1 ulp. Scores here are bounded metric values
    /// in [0, 1], so the deviation never materialises in practice.
    fn mean(&self, pick: fn(&QueryResult) -> f64) -> f64 {
        if self.queries.is_empty() {
            return 0.0;
        }
        neumaier_sum(&self.queries.iter().map(pick).collect::<Vec<_>>()) / self.queries.len() as f64
    }

    pub fn recall(&self) -> f64 {
        self.mean(|q| q.recall)
    }

    pub fn precision(&self) -> f64 {
        self.mean(|q| q.precision)
    }

    pub fn mrr(&self) -> f64 {
        self.mean(|q| q.reciprocal_rank)
    }

    pub fn ndcg(&self) -> f64 {
        self.mean(|q| q.ndcg)
    }

    pub fn empty_queries(&self) -> usize {
        self.queries.iter().filter(|q| q.empty).count()
    }

    /// Mirrors `RunResult.summary()`: means rounded to 4 decimals.
    ///
    /// Deviation note: Python `round(x, 4)` is round-half-even; [`round4`]
    /// is round-half-away. The two agree unless a mean lands exactly on a
    /// 5-decimal tie, which float means of bounded scores do not hit.
    pub fn summary(&self) -> RunSummary {
        RunSummary {
            config: self.config.clone(),
            queries: self.queries.len(),
            k: self.k,
            recall: round4(self.recall()),
            precision: round4(self.precision()),
            mrr: round4(self.mrr()),
            ndcg: round4(self.ndcg()),
            empty: self.empty_queries(),
        }
    }
}

/// Neumaier-compensated summation (see [`RunResult::mean`]).
fn neumaier_sum(values: &[f64]) -> f64 {
    let mut sum = 0.0;
    let mut compensation = 0.0;
    for &value in values {
        let next = sum + value;
        if sum.abs() >= value.abs() {
            compensation += (sum - next) + value;
        } else {
            compensation += (value - next) + sum;
        }
        sum = next;
    }
    sum + compensation
}

fn round4(value: f64) -> f64 {
    (value * 10_000.0).round() / 10_000.0
}

/// The `summary()` row. `to_map` renders the exact Python key shape
/// (`recall@k`, `precision@k`, `mrr`, `ndcg@k` plus `config`/`queries`/
/// `empty`); field order is never inherited — maps sort explicitly.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunSummary {
    pub config: String,
    pub queries: usize,
    pub k: usize,
    pub recall: f64,
    pub precision: f64,
    pub mrr: f64,
    pub ndcg: f64,
    pub empty: usize,
}

impl RunSummary {
    pub fn to_map(&self) -> std::collections::BTreeMap<String, serde_json::Value> {
        let mut map = std::collections::BTreeMap::new();
        map.insert(
            "config".to_string(),
            serde_json::Value::String(self.config.clone()),
        );
        map.insert("queries".to_string(), serde_json::json!(self.queries));
        map.insert(format!("recall@{}", self.k), serde_json::json!(self.recall));
        map.insert(
            format!("precision@{}", self.k),
            serde_json::json!(self.precision),
        );
        map.insert("mrr".to_string(), serde_json::json!(self.mrr));
        map.insert(format!("ndcg@{}", self.k), serde_json::json!(self.ndcg));
        map.insert("empty".to_string(), serde_json::json!(self.empty));
        map
    }
}

/// One row of [`compare`]: a run's summary plus, for every non-baseline run,
/// the paired per-metric diffs against the first run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CompareRow {
    pub config: String,
    pub queries: usize,
    pub k: usize,
    pub recall: f64,
    pub precision: f64,
    pub mrr: f64,
    pub ndcg: f64,
    pub empty: usize,
    /// `None` for the baseline (first) run.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delta_recall: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delta_precision: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delta_mrr: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delta_ndcg: Option<f64>,
}

impl CompareRow {
    /// Exact Python dict shape: the summary keys plus `Δ <metric>` entries
    /// for non-baseline rows.
    pub fn to_map(&self) -> std::collections::BTreeMap<String, serde_json::Value> {
        let summary = RunSummary {
            config: self.config.clone(),
            queries: self.queries,
            k: self.k,
            recall: self.recall,
            precision: self.precision,
            mrr: self.mrr,
            ndcg: self.ndcg,
            empty: self.empty,
        };
        let mut map = summary.to_map();
        if let Some(delta) = self.delta_recall {
            map.insert(format!("Δ recall@{}", self.k), serde_json::json!(delta));
        }
        if let Some(delta) = self.delta_precision {
            map.insert(format!("Δ precision@{}", self.k), serde_json::json!(delta));
        }
        if let Some(delta) = self.delta_mrr {
            map.insert("Δ mrr".to_string(), serde_json::json!(delta));
        }
        if let Some(delta) = self.delta_ndcg {
            map.insert(format!("Δ ndcg@{}", self.k), serde_json::json!(delta));
        }
        map
    }
}

/// Paired per-metric diffs against the first run.
///
/// Absolute numbers on a hand-built query set mean little; the movement
/// between two configurations is the signal. Empty input yields no rows.
pub fn compare(runs: &[RunResult]) -> Vec<CompareRow> {
    let Some((baseline, rest)) = runs.split_first() else {
        return Vec::new();
    };
    let baseline_summary = baseline.summary();
    let mut rows = vec![CompareRow {
        config: baseline_summary.config.clone(),
        queries: baseline_summary.queries,
        k: baseline_summary.k,
        recall: baseline_summary.recall,
        precision: baseline_summary.precision,
        mrr: baseline_summary.mrr,
        ndcg: baseline_summary.ndcg,
        empty: baseline_summary.empty,
        delta_recall: None,
        delta_precision: None,
        delta_mrr: None,
        delta_ndcg: None,
    }];
    for run in rest {
        let summary = run.summary();
        rows.push(CompareRow {
            config: summary.config.clone(),
            queries: summary.queries,
            k: summary.k,
            recall: summary.recall,
            precision: summary.precision,
            mrr: summary.mrr,
            ndcg: summary.ndcg,
            empty: summary.empty,
            delta_recall: Some(round4(run.recall() - baseline.recall())),
            delta_precision: Some(round4(run.precision() - baseline.precision())),
            delta_mrr: Some(round4(run.mrr() - baseline.mrr())),
            delta_ndcg: Some(round4(run.ndcg() - baseline.ndcg())),
        });
    }
    rows
}

// ---------------------------------------------------------------------------
// Tests: mirror tests/unit/eval/test_metrics.py case-for-case, plus the
// runner summary/compare contract and the extra edge guards named in the
// port ticket (k == 0, graded-vs-binary, ideal-zero).
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn pid(n: u128) -> Uuid {
        Uuid::from_u128(n)
    }

    fn approx(actual: f64, expected: f64) -> bool {
        (actual - expected).abs() < 1e-9
    }

    const A: u128 = 1;
    const B: u128 = 2;
    const C: u128 = 3;
    const D: u128 = 4;
    const E: u128 = 5;

    fn ids(ns: &[u128]) -> Vec<Uuid> {
        ns.iter().map(|n| pid(*n)).collect()
    }

    // -- recall (TestRecall) ---------------------------------------------

    #[test]
    fn recall_all_relevant_retrieved() {
        assert_eq!(recall_at_k(&ids(&[A, B, C]), &ids(&[A, B]), 3), 1.0);
    }

    #[test]
    fn recall_half_retrieved() {
        assert_eq!(recall_at_k(&ids(&[A, D, E]), &ids(&[A, B]), 3), 0.5);
    }

    #[test]
    fn recall_cutoff_excludes_later_hits() {
        assert_eq!(recall_at_k(&ids(&[D, E, A]), &ids(&[A]), 2), 0.0);
        assert_eq!(recall_at_k(&ids(&[D, E, A]), &ids(&[A]), 3), 1.0);
    }

    #[test]
    fn recall_no_relevant_passages_is_not_a_failure() {
        assert_eq!(recall_at_k(&ids(&[A, B]), &[], 2), 1.0);
    }

    #[test]
    fn recall_empty_retrieval() {
        assert_eq!(recall_at_k(&[], &ids(&[A]), 10), 0.0);
    }

    #[test]
    fn recall_duplicate_retrieved_ids_count_once() {
        // Set semantics: set(retrieved[:k]) & relevant.
        assert_eq!(recall_at_k(&ids(&[A, A, A]), &ids(&[A, B]), 3), 0.5);
    }

    // -- precision (TestPrecision) ----------------------------------------

    #[test]
    fn precision_counts_against_k_not_result_length() {
        assert!(approx(
            precision_at_k(&ids(&[A, B]), &ids(&[A, B]), 10),
            0.2
        ));
    }

    #[test]
    fn precision_full() {
        assert_eq!(precision_at_k(&ids(&[A, B]), &ids(&[A, B]), 2), 1.0);
    }

    #[test]
    fn precision_zero_k_is_zero() {
        // Python: `if k <= 0: return 0.0`; negative k is unrepresentable
        // as usize, so k == 0 exercises the guard.
        assert_eq!(precision_at_k(&ids(&[A, B]), &ids(&[A, B]), 0), 0.0);
    }

    // -- reciprocal rank (TestReciprocalRank) ------------------------------

    #[test]
    fn reciprocal_rank_first_position() {
        assert_eq!(reciprocal_rank(&ids(&[A, B]), &ids(&[A])), 1.0);
    }

    #[test]
    fn reciprocal_rank_third_position() {
        assert!(approx(
            reciprocal_rank(&ids(&[D, E, A]), &ids(&[A])),
            1.0 / 3.0
        ));
    }

    #[test]
    fn reciprocal_rank_uses_the_first_relevant_only() {
        assert_eq!(reciprocal_rank(&ids(&[D, A, B]), &ids(&[A, B])), 0.5);
    }

    #[test]
    fn reciprocal_rank_nothing_relevant() {
        assert_eq!(reciprocal_rank(&ids(&[D, E]), &ids(&[A])), 0.0);
    }

    // -- DCG / nDCG (TestNDCG) ----------------------------------------------

    #[test]
    fn dcg_matches_the_definition() {
        // 1/log2(2) + 1/log2(3) = 1 + 0.6309…
        assert!(approx(dcg(&[1.0, 1.0]), 1.0 + 1.0 / 3.0f64.log2()));
    }

    #[test]
    fn ndcg_perfect_ranking_scores_one() {
        assert!(approx(
            ndcg_at_k(&ids(&[A, B, C]), NdcgRelevant::Binary(&ids(&[A, B, C])), 3),
            1.0
        ));
    }

    #[test]
    fn ndcg_order_matters() {
        let good = ndcg_at_k(&ids(&[A, D, E]), NdcgRelevant::Binary(&ids(&[A])), 3);
        let bad = ndcg_at_k(&ids(&[D, E, A]), NdcgRelevant::Binary(&ids(&[A])), 3);
        assert!(good > bad);
        assert!(approx(good, 1.0));
        assert!(approx(bad, 1.0 / 4.0f64.log2()));
    }

    #[test]
    fn ndcg_graded_relevance_rewards_putting_the_best_first() {
        let grades = [(pid(A), 3.0), (pid(B), 1.0)];
        let best_first = ndcg_at_k(&ids(&[A, B]), NdcgRelevant::Graded(&grades), 2);
        let worst_first = ndcg_at_k(&ids(&[B, A]), NdcgRelevant::Graded(&grades), 2);
        assert!(best_first > worst_first);
    }

    #[test]
    fn ndcg_graded_perfect_order_scores_one() {
        let grades = [(pid(A), 3.0), (pid(B), 1.0)];
        assert!(approx(
            ndcg_at_k(&ids(&[A, B]), NdcgRelevant::Graded(&grades), 2),
            1.0
        ));
    }

    #[test]
    fn ndcg_irrelevant_results_contribute_nothing() {
        let grades = [(pid(A), 1.0)];
        assert_eq!(
            ndcg_at_k(&ids(&[D, E]), NdcgRelevant::Graded(&grades), 2),
            0.0
        );
    }

    #[test]
    fn ndcg_no_judgments_is_not_a_failure() {
        assert_eq!(ndcg_at_k(&ids(&[A]), NdcgRelevant::Binary(&[]), 5), 1.0);
        let empty: [(Uuid, f64); 0] = [];
        assert_eq!(ndcg_at_k(&ids(&[A]), NdcgRelevant::Graded(&empty), 5), 1.0);
    }

    #[test]
    fn ndcg_zero_ideal_scores_zero() {
        // Non-empty judgments whose ideal DCG is 0 (all grades 0.0):
        // `actual / ideal if ideal else 0.0`.
        let grades = [(pid(A), 0.0)];
        assert_eq!(ndcg_at_k(&ids(&[A]), NdcgRelevant::Graded(&grades), 1), 0.0);
    }

    // -- queryset shapes -----------------------------------------------------

    #[test]
    fn queryset_defaults_match_python() {
        let set: QuerySet = serde_json::from_value(serde_json::json!({
            "name": "smoke",
            "queries": [{"query": "q", "relevant": {}}],
        }))
        .unwrap();
        assert_eq!(set.version, "1");
        assert_eq!(set.description, None);
        assert_eq!(set.len(), 1);
        assert!(!set.is_empty());
        assert!(set.queries[0].relevant_ids().is_empty());
    }

    #[test]
    fn relevant_ids_sorted_is_canonical() {
        let query = EvalQuery {
            query: "q".to_string(),
            relevant: [(pid(B), 1.0), (pid(A), 1.0)].into_iter().collect(),
            note: None,
            filters: None,
        };
        assert_eq!(query.relevant_ids_sorted(), vec![pid(A), pid(B)]);
    }

    // -- runner: score_query / summary / compare ------------------------------

    fn judged() -> EvalQuery {
        EvalQuery {
            query: "q".to_string(),
            relevant: [(pid(A), 1.0), (pid(B), 1.0)].into_iter().collect(),
            note: None,
            filters: None,
        }
    }

    #[test]
    fn score_query_marks_empty_retrieval() {
        let result = score_query("q", vec![], &judged(), 10);
        assert!(result.empty);
        assert_eq!(result.recall, 0.0);
        assert_eq!(result.ndcg, 0.0);
    }

    #[test]
    fn score_query_scores_like_run_queryset_body() {
        let result = score_query("q", ids(&[A, D]), &judged(), 10);
        assert!(!result.empty);
        assert_eq!(result.recall, 0.5);
        assert_eq!(result.precision, 0.1);
        assert_eq!(result.reciprocal_rank, 1.0);
    }

    #[test]
    fn run_summary_rounds_means_and_counts_empty() {
        let run = RunResult {
            config: "default".to_string(),
            k: 10,
            queries: vec![
                score_query("q1", ids(&[A, B]), &judged(), 10),
                score_query("q2", vec![], &judged(), 10),
            ],
        };
        assert!(approx(run.recall(), 0.5));
        assert!(approx(run.precision(), 0.1));
        assert!(approx(run.mrr(), 0.5));
        assert_eq!(run.empty_queries(), 1);
        let summary = run.summary();
        assert_eq!(summary.config, "default");
        assert_eq!(summary.queries, 2);
        assert_eq!(summary.empty, 1);
        let map = summary.to_map();
        assert_eq!(map["config"], serde_json::json!("default"));
        assert!(map.contains_key("recall@10"));
        assert!(map.contains_key("precision@10"));
        assert!(map.contains_key("ndcg@10"));
        assert!(map.contains_key("mrr"));
    }

    #[test]
    fn run_mean_is_zero_with_no_queries() {
        let run = RunResult {
            config: "default".to_string(),
            k: 10,
            queries: vec![],
        };
        assert_eq!(run.recall(), 0.0);
        assert_eq!(run.mrr(), 0.0);
        assert_eq!(run.ndcg(), 0.0);
        assert_eq!(run.empty_queries(), 0);
    }

    #[test]
    fn compare_diffs_against_first_run() {
        let baseline = RunResult {
            config: "a".to_string(),
            k: 10,
            queries: vec![score_query("q", ids(&[A, D]), &judged(), 10)],
        };
        let improved = RunResult {
            config: "b".to_string(),
            k: 10,
            queries: vec![score_query("q", ids(&[A, B]), &judged(), 10)],
        };
        let rows = compare(&[baseline, improved]);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].delta_recall, None);
        assert_eq!(rows[1].delta_recall, Some(0.5));
        assert!(rows[1].delta_mrr.unwrap() >= 0.0);
        let map = rows[1].to_map();
        assert_eq!(map["Δ recall@10"], serde_json::json!(0.5));
        assert!(map.contains_key("Δ mrr"));
        // The baseline row carries no deltas: exercises the `None` arms.
        let base_map = rows[0].to_map();
        assert!(!base_map.keys().any(|key| key.starts_with('Δ')));
    }

    #[test]
    fn compare_empty_runs_yields_no_rows() {
        assert!(compare(&[]).is_empty());
    }
}
