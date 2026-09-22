//! `marginalia_rs.chunk` — signature-identical wrappers over `marginalia-chunk`.
//!
//! Identity notes (all pinned by tests):
//! - Fused rows return the *original* pid objects, so downstream code (dict
//!   lookups keyed by `UUID`, `get_many`) cannot tell the backend.
//! - `k` on `rrf_fuse` is accepted and ignored: upstream threads it but the
//!   divisor is `RRF_K`. Kept so the seam cannot silently "fix" scores.
//! - Breakdown keys emit in ascending list order, matching the Python dict
//!   insertion order; weighted breakdowns emit `vector_norm` then
//!   `keyword_norm`, as upstream builds them.
//! - Pids must be UUIDs (or their canonical strings): the typed contract at
//!   every call site is `list[tuple[UUID, float]]`. Anything else answers
//!   `ValueError`; upstream would accept any hashable there.
//! - Exact-score ties in `weighted_fuse` order vector-then-keyword here while
//!   upstream iterates a `set` (order varies run to run): scores per id are
//!   the contract there, not row order. NaN scores are outside the proven
//!   domain on both sides of this seam.

use std::collections::HashMap;

use marginalia_chunk::fusion as chunk_fusion;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use uuid::Uuid;

/// Parse one hit list, keeping each row's original pid object.
///
/// # Errors
///
/// Returns `ValueError` when a pid is not a UUID.
fn key_hits(hits: Vec<(Bound<'_, PyAny>, f64)>) -> PyResult<Vec<(Uuid, Py<PyAny>, f64)>> {
    hits.into_iter()
        .map(|(obj, score)| {
            let text: String = obj.str().expect("pids stringify").to_string();
            let key = Uuid::parse_str(&text).map_err(|_| {
                PyValueError::new_err(format!(
                    "marginalia_rs.chunk expects UUID pids, got {text:?}"
                ))
            })?;
            Ok((key, obj.unbind(), score))
        })
        .collect()
}

/// Reciprocal Rank Fusion across ranked lists.
///
/// Mirrors `services/search/fusion.py::rrf_fuse` exactly, including the
/// ignored `k`.
///
/// # Errors
///
/// Returns `ValueError` when a pid is not a UUID.
#[pyfunction]
#[pyo3(signature = (*ranked_lists, k = 60))]
fn rrf_fuse<'py>(
    py: Python<'py>,
    ranked_lists: Vec<Vec<(Bound<'py, PyAny>, f64)>>,
    k: i64,
) -> PyResult<Bound<'py, PyList>> {
    // Upstream threads `k` but the divisor is `RRF_K`; accepting-and-ignoring
    // keeps the signature identical so the seam cannot silently "fix" scores.
    let _ = k;
    let keyed: Vec<Vec<(Uuid, Py<PyAny>, f64)>> = ranked_lists
        .into_iter()
        .map(key_hits)
        .collect::<PyResult<_>>()?;
    let views: Vec<Vec<(Uuid, f64)>> = keyed
        .iter()
        .map(|hits| hits.iter().map(|(key, _, score)| (*key, *score)).collect())
        .collect();
    let fused = chunk_fusion::rrf_fuse(&views);
    let mut first_seen: HashMap<Uuid, Py<PyAny>> = HashMap::new();
    for hits in &keyed {
        for (key, obj, _) in hits {
            first_seen.entry(*key).or_insert_with(|| obj.clone_ref(py));
        }
    }
    let out = PyList::empty(py);
    for (pid, score, breakdown) in &fused {
        let bd = PyDict::new(py);
        for idx in 0..views.len() {
            let list_key = format!("list_{idx}");
            if let Some(contrib) = breakdown.get(&list_key) {
                let entry = PyDict::new(py);
                entry
                    .set_item("rank", contrib.rank)
                    .expect("dict attribute assignment");
                entry
                    .set_item("score", contrib.score)
                    .expect("dict attribute assignment");
                bd.set_item(list_key, entry)
                    .expect("dict attribute assignment");
            }
        }
        let obj = first_seen
            .get(pid)
            .expect("fused ids come from the inputs")
            .bind(py);
        out.append((obj, score, bd))
            .expect("list of converted rows appends");
    }
    Ok(out)
}

/// Weighted-sum fusion with min-max normalization.
///
/// Mirrors `services/search/fusion.py::weighted_fuse` exactly, except
/// exact-score ties keep vector-then-keyword order (see module notes).
///
/// # Errors
///
/// Returns `ValueError` when a pid is not a UUID.
#[pyfunction]
#[pyo3(signature = (vec_hits, kw_hits, alpha = 0.5))]
fn weighted_fuse<'py>(
    py: Python<'py>,
    vec_hits: Vec<(Bound<'py, PyAny>, f64)>,
    kw_hits: Vec<(Bound<'py, PyAny>, f64)>,
    alpha: f64,
) -> PyResult<Bound<'py, PyList>> {
    let vec_keyed = key_hits(vec_hits)?;
    let kw_keyed = key_hits(kw_hits)?;
    let vec_view: Vec<(Uuid, f64)> = vec_keyed
        .iter()
        .map(|(key, _, score)| (*key, *score))
        .collect();
    let kw_view: Vec<(Uuid, f64)> = kw_keyed
        .iter()
        .map(|(key, _, score)| (*key, *score))
        .collect();
    let fused = chunk_fusion::weighted_fuse(&vec_view, &kw_view, alpha);
    let mut first_seen: HashMap<Uuid, Py<PyAny>> = HashMap::new();
    for (key, obj, _) in vec_keyed.iter().chain(kw_keyed.iter()) {
        first_seen.entry(*key).or_insert_with(|| obj.clone_ref(py));
    }
    let out = PyList::empty(py);
    for (pid, score, breakdown) in &fused {
        let bd = PyDict::new(py);
        bd.set_item("vector_norm", breakdown.vector_norm)
            .expect("dict attribute assignment");
        bd.set_item("keyword_norm", breakdown.keyword_norm)
            .expect("dict attribute assignment");
        let obj = first_seen
            .get(pid)
            .expect("fused ids come from the inputs")
            .bind(py);
        out.append((obj, score, bd))
            .expect("list of converted rows appends");
    }
    Ok(out)
}

pub fn chunk_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "chunk").expect("module name is a valid literal");
    m.add_function(wrap_pyfunction!(rrf_fuse, &m).expect("function name is a unique literal"))
        .expect("module attribute assignment");
    m.add_function(wrap_pyfunction!(weighted_fuse, &m).expect("function name is a unique literal"))
        .expect("module attribute assignment");
    m.add("RRF_K", chunk_fusion::RRF_K)
        .expect("module attribute assignment");
    super::windows::register_windows(&m);
    super::chunkers::register_chunkers(&m);
    m
}

#[cfg(test)]
mod tests {
    use super::{chunk_module, rrf_fuse, weighted_fuse};
    use marginalia_chunk::fusion as chunk_fusion;
    use pyo3::prelude::*;
    use pyo3::types::PyDict;
    use std::collections::HashMap;
    use uuid::Uuid;

    fn hits<'py>(py: Python<'py>, rows: &[(String, f64)]) -> Vec<(Bound<'py, PyAny>, f64)> {
        rows.iter()
            .map(|(id, score)| (id.clone().into_pyobject(py).unwrap().into_any(), *score))
            .collect()
    }

    fn uuids(n: usize) -> Vec<String> {
        (0..n).map(|_| Uuid::new_v4().to_string()).collect()
    }

    #[test]
    fn rrf_matches_crate_bit_exact_with_ordered_breakdowns() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let ids = uuids(3);
            let vec_rows = vec![(ids[0].clone(), 0.95), (ids[1].clone(), 0.8)];
            let kw_rows = vec![(ids[1].clone(), 0.9), (ids[2].clone(), 0.7)];
            let out = rrf_fuse(py, vec![hits(py, &vec_rows), hits(py, &kw_rows)], 60).unwrap();
            let parsed: Vec<Vec<(Uuid, f64)>> = [vec_rows, kw_rows]
                .iter()
                .map(|rows| {
                    rows.iter()
                        .map(|(id, s)| (Uuid::parse_str(id).unwrap(), *s))
                        .collect()
                })
                .collect();
            let expected = chunk_fusion::rrf_fuse(&parsed);
            assert_eq!(out.len(), expected.len());
            for (i, (pid, score, breakdown)) in expected.iter().enumerate() {
                let row: (Bound<'_, PyAny>, f64, Bound<'_, PyDict>) =
                    out.get_item(i).unwrap().extract().unwrap();
                assert_eq!(row.0.str().unwrap().to_string(), pid.to_string());
                assert_eq!(row.1.to_bits(), score.to_bits());
                let keys: Vec<String> = row.2.keys().iter().map(|k| k.extract().unwrap()).collect();
                let mut want: Vec<String> = breakdown.keys().cloned().collect();
                want.sort();
                assert_eq!(keys, want);
                for (list_key, contrib) in breakdown {
                    let entry: Bound<'_, PyDict> = row
                        .2
                        .get_item(list_key)
                        .unwrap()
                        .unwrap()
                        .extract()
                        .unwrap();
                    assert_eq!(
                        entry
                            .get_item("rank")
                            .unwrap()
                            .unwrap()
                            .extract::<i64>()
                            .unwrap(),
                        contrib.rank
                    );
                    assert_eq!(
                        entry
                            .get_item("score")
                            .unwrap()
                            .unwrap()
                            .extract::<f64>()
                            .unwrap()
                            .to_bits(),
                        contrib.score.to_bits()
                    );
                }
            }
        });
    }

    #[test]
    fn rrf_k_is_accepted_and_ignored() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let ids = uuids(2);
            let lists = vec![
                hits(py, &[(ids[0].clone(), 0.9)]),
                hits(py, &[(ids[1].clone(), 0.8)]),
            ];
            let defaulted = rrf_fuse(py, lists.clone(), 60).unwrap().to_string();
            let seven = rrf_fuse(py, lists, 7).unwrap().to_string();
            assert_eq!(defaulted, seven);
            let empty: Vec<Vec<(Bound<'_, PyAny>, f64)>> = vec![vec![], vec![]];
            assert_eq!(rrf_fuse(py, empty, 60).unwrap().len(), 0);
        });
    }

    #[test]
    fn weighted_matches_crate_with_ties_compared_per_id() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let ids = uuids(3);
            let vec_rows = vec![(ids[0].clone(), 0.9), (ids[1].clone(), 0.5)];
            let kw_rows = vec![(ids[1].clone(), 1.4), (ids[2].clone(), 0.2)];
            let out = weighted_fuse(py, hits(py, &vec_rows), hits(py, &kw_rows), 0.3).unwrap();
            let parse = |rows: &[(String, f64)]| {
                rows.iter()
                    .map(|(id, s)| (Uuid::parse_str(id).unwrap(), *s))
                    .collect::<Vec<_>>()
            };
            let expected = chunk_fusion::weighted_fuse(&parse(&vec_rows), &parse(&kw_rows), 0.3);
            let mut got: HashMap<String, (u64, u64, u64)> = HashMap::new();
            for i in 0..out.len() {
                let row: (Bound<'_, PyAny>, f64, Bound<'_, PyDict>) =
                    out.get_item(i).unwrap().extract().unwrap();
                let bd = row.2;
                got.insert(
                    row.0.str().unwrap().to_string(),
                    (
                        row.1.to_bits(),
                        bd.get_item("vector_norm")
                            .unwrap()
                            .unwrap()
                            .extract::<f64>()
                            .unwrap()
                            .to_bits(),
                        bd.get_item("keyword_norm")
                            .unwrap()
                            .unwrap()
                            .extract::<f64>()
                            .unwrap()
                            .to_bits(),
                    ),
                );
            }
            assert_eq!(got.len(), expected.len());
            for (pid, score, breakdown) in &expected {
                let (bits, vs, ks) = &got[&pid.to_string()];
                assert_eq!(*bits, score.to_bits());
                assert_eq!(*vs, breakdown.vector_norm.to_bits());
                assert_eq!(*ks, breakdown.keyword_norm.to_bits());
            }
            // Exact tie: single-sided singleton lists normalize to 0.0 both sides.
            let tie = weighted_fuse(
                py,
                hits(py, &[(ids[0].clone(), 1.0)]),
                hits(py, &[(ids[1].clone(), 2.0)]),
                0.5,
            )
            .unwrap();
            assert_eq!(tie.len(), 2);
            // Empty inputs fuse to nothing, on either side.
            let none: Vec<(Bound<'_, PyAny>, f64)> = vec![];
            assert_eq!(weighted_fuse(py, none.clone(), none, 0.5).unwrap().len(), 0);
        });
    }

    #[test]
    fn non_uuid_pids_are_value_errors_on_every_entry_point() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let bad = hits(py, &[("not-a-uuid".to_owned(), 1.0)]);
            let good = hits(py, &[(Uuid::new_v4().to_string(), 1.0)]);
            assert!(rrf_fuse(py, vec![bad.clone()], 60).is_err());
            assert!(rrf_fuse(py, vec![good.clone(), bad.clone()], 60).is_err());
            assert!(weighted_fuse(py, bad.clone(), good.clone(), 0.5).is_err());
            assert!(weighted_fuse(py, good, bad, 0.5).is_err());
        });
    }

    #[test]
    fn uuid_objects_round_trip_as_uuid_objects() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let uuid_mod = PyModule::import(py, "uuid").unwrap();
            let mk = |n: u32| {
                uuid_mod
                    .getattr("UUID")
                    .unwrap()
                    .call1((format!("12345678-1234-5678-1234-5678123456{n:02}"),))
                    .unwrap()
                    .to_owned()
            };
            let rows = vec![(mk(1), 0.9), (mk(2), 0.4)];
            let out = weighted_fuse(py, rows, vec![], 0.5).unwrap();
            assert_eq!(out.len(), 2);
            for i in 0..2 {
                let row: (Bound<'_, PyAny>, f64, Bound<'_, PyDict>) =
                    out.get_item(i).unwrap().extract().unwrap();
                assert_eq!(row.0.get_type().name().unwrap().to_string(), "UUID");
            }
            let _ = chunk_module(py);
        });
    }
}
