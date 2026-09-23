//! Retrieval-evaluation bindings over `marginalia-ret`.
//!
//! Crossing contract (all pinned by tests):
//! - Passage ids cross as canonical UUID strings (parsed back with
//!   `Uuid::parse_str`; anything else answers `ValueError`). Outputs are
//!   floats — no identity to preserve.
//! - `ndcg_at_k` takes relevance either as an id list (binary) or as an
//!   id→grade mapping, mirroring the Python `isinstance` branch; both arms
//!   are fired by tests.
//! - Floats cross as binary `f64`. `recall`/`precision`/`reciprocal_rank`
//!   are exactly rounded IEEE division on both sides; `dcg` is a plain
//!   fold where CPython uses Neumaier-compensated `sum()` (documented in
//!   the crate: it feeds comparisons, never truncation), so long gain
//!   lists may differ by 1 ulp — the parity suite measures the boundary
//!   rather than asserting blind bit-equality there.

use marginalia_ret::eval as ret_eval;
use pyo3::exceptions::PyValueError;

use pyo3::prelude::*;
use pyo3::types::PyDict;
use uuid::Uuid;

/// Parse canonical UUID strings.
///
/// # Errors
///
/// Returns `ValueError` on the first unparseable id.
fn parse_ids(ids: &[String]) -> PyResult<Vec<Uuid>> {
    ids.iter()
        .map(|s| {
            Uuid::parse_str(s).map_err(|_| {
                PyValueError::new_err(format!("marginalia_rs.ret expects UUID ids, got {s:?}"))
            })
        })
        .collect()
}

/// Fraction of relevant passages in the top *k* (1.0 when none judged).
/// Mirrors `eval/metrics.py::recall_at_k` exactly.
#[pyfunction]
fn recall_at_k(retrieved: Vec<String>, relevant: Vec<String>, k: usize) -> PyResult<f64> {
    Ok(ret_eval::recall_at_k(
        &parse_ids(&retrieved)?,
        &parse_ids(&relevant)?,
        k,
    ))
}

/// Fraction of the top *k* that is relevant (0.0 when `k == 0`).
/// Mirrors `eval/metrics.py::precision_at_k` exactly.
#[pyfunction]
fn precision_at_k(retrieved: Vec<String>, relevant: Vec<String>, k: usize) -> PyResult<f64> {
    Ok(ret_eval::precision_at_k(
        &parse_ids(&retrieved)?,
        &parse_ids(&relevant)?,
        k,
    ))
}

/// 1/rank of the first relevant result; 0.0 if none is retrieved.
/// Mirrors `eval/metrics.py::reciprocal_rank` exactly.
#[pyfunction]
fn reciprocal_rank(retrieved: Vec<String>, relevant: Vec<String>) -> PyResult<f64> {
    Ok(ret_eval::reciprocal_rank(
        &parse_ids(&retrieved)?,
        &parse_ids(&relevant)?,
    ))
}

/// Discounted cumulative gain over `gains`.
/// Mirrors `eval/metrics.py::dcg` up to the documented fold-vs-Neumaier
/// boundary (see module notes).
#[pyfunction]
fn dcg(gains: Vec<f64>) -> f64 {
    // `Iterator::sum` answers `-0.0` on an empty fold while Python's
    // `sum([])` is int `0`: `+ 0.0` canonicalizes the zero without
    // touching any other value (IEEE identity except on `-0.0`).
    ret_eval::dcg(&gains) + 0.0
}

/// Normalised discounted cumulative gain over the top *k*.
///
/// `relevant` is an id list (binary) or an id→grade mapping, exactly like
/// upstream. Mirrors `eval/metrics.py::ndcg_at_k` up to the same boundary.
///
/// # Errors
///
/// Returns `ValueError` on unparseable ids.
#[pyfunction]
fn ndcg_at_k(retrieved: Vec<String>, relevant: Bound<'_, PyAny>, k: usize) -> PyResult<f64> {
    let retrieved = parse_ids(&retrieved)?;
    if let Ok(mapping) = relevant.downcast::<PyDict>() {
        let mut pairs: Vec<(Uuid, f64)> = Vec::new();
        for (key, value) in mapping.iter() {
            let id: String = key.extract().map_err(|_| {
                PyValueError::new_err("marginalia_rs.ret expects UUID ids".to_owned())
            })?;
            let grade: f64 = value.extract().map_err(|_| {
                PyValueError::new_err("marginalia_rs.ret expects float grades".to_owned())
            })?;
            pairs.push((
                Uuid::parse_str(&id).map_err(|_| {
                    PyValueError::new_err(format!("marginalia_rs.ret expects UUID ids, got {id:?}"))
                })?,
                grade,
            ));
        }
        Ok(ret_eval::ndcg_at_k(
            &retrieved,
            ret_eval::NdcgRelevant::Graded(&pairs),
            k,
        ))
    } else {
        let ids: Vec<String> = relevant.extract().map_err(|_| {
            PyValueError::new_err(
                "marginalia_rs.ret expects an id list or id→grade mapping".to_owned(),
            )
        })?;
        Ok(ret_eval::ndcg_at_k(
            &retrieved,
            ret_eval::NdcgRelevant::Binary(&parse_ids(&ids)?),
            k,
        ))
    }
}

pub fn ret_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "ret").expect("module name is a valid literal");
    register_ret(&m);
    m
}

pub fn register_ret(m: &Bound<'_, PyModule>) {
    for f in [
        wrap_pyfunction!(recall_at_k, m).expect("function name is a unique literal"),
        wrap_pyfunction!(precision_at_k, m).expect("function name is a unique literal"),
        wrap_pyfunction!(reciprocal_rank, m).expect("function name is a unique literal"),
        wrap_pyfunction!(dcg, m).expect("function name is a unique literal"),
        wrap_pyfunction!(ndcg_at_k, m).expect("function name is a unique literal"),
    ] {
        m.add_function(f).expect("module attribute assignment");
    }
}

#[cfg(test)]
mod tests {
    use super::{dcg, ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank, register_ret};
    use pyo3::prelude::*;
    use pyo3::types::{PyDict, PyList};

    fn ids(n: usize) -> Vec<String> {
        (0..n)
            .map(|i| format!("12345678-1234-5678-1234-56781234{i:04}"))
            .collect()
    }

    #[test]
    fn metrics_match_crate_on_rank_shapes() {
        let all = ids(10);
        let retrieved = all[..6].to_vec();
        let relevant = all[2..8].to_vec();
        assert_eq!(
            recall_at_k(retrieved.clone(), relevant.clone(), 10)
                .unwrap()
                .to_bits(),
            marginalia_ret::eval::recall_at_k(&parsed(&retrieved), &parsed(&relevant), 10)
                .to_bits()
        );
        assert_eq!(
            precision_at_k(retrieved.clone(), relevant.clone(), 0)
                .unwrap()
                .to_bits(),
            0.0f64.to_bits()
        );
        assert_eq!(
            reciprocal_rank(retrieved.clone(), relevant.clone())
                .unwrap()
                .to_bits(),
            marginalia_ret::eval::reciprocal_rank(&parsed(&retrieved), &parsed(&relevant))
                .to_bits()
        );
        assert!(recall_at_k(vec!["nope".to_owned()], relevant.clone(), 10).is_err());
        assert!(recall_at_k(retrieved.clone(), vec!["nope".to_owned()], 10).is_err());
        assert!(precision_at_k(retrieved.clone(), vec!["nope".to_owned()], 10).is_err());
        assert!(precision_at_k(vec!["nope".to_owned()], relevant.clone(), 10).is_err());
        assert!(reciprocal_rank(retrieved.clone(), vec!["nope".to_owned()]).is_err());
        assert!(reciprocal_rank(vec!["nope".to_owned()], relevant.clone()).is_err());
        assert!(recall_at_k(vec!["nope".to_owned()], relevant, 10).is_err());
    }

    fn parsed(ids: &[String]) -> Vec<uuid::Uuid> {
        ids.iter()
            .map(|s| uuid::Uuid::parse_str(s).unwrap())
            .collect()
    }

    #[test]
    fn dcg_matches_crate_on_short_lists() {
        // Empty canonicalizes `-0.0` (fold) to `+0.0` (Python `sum`); the
        // rest mirror the crate bit-for-bit.
        assert_eq!(dcg(vec![]).to_bits(), 0.0f64.to_bits());
        for gains in [vec![3.0], vec![3.0, 2.0, 1.0], vec![0.5; 20]] {
            assert_eq!(
                dcg(gains.clone()).to_bits(),
                marginalia_ret::eval::dcg(&gains).to_bits()
            );
        }
    }

    #[test]
    fn ndcg_handles_binary_and_graded() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let all = ids(6);
            let retrieved = all[..4].to_vec();
            let binary: Vec<String> = all[1..5].to_vec();
            let as_any: Bound<'_, PyAny> = PyList::new(py, &binary).unwrap().into_any();
            let got = ndcg_at_k(retrieved.clone(), as_any, 10).unwrap();
            let want = marginalia_ret::eval::ndcg_at_k(
                &parsed(&retrieved),
                marginalia_ret::eval::NdcgRelevant::Binary(&parsed(&binary)),
                10,
            );
            assert_eq!(got.to_bits(), want.to_bits());
            let graded = PyDict::new(py);
            graded.set_item(&all[1], 2.0).unwrap();
            graded.set_item(&all[2], 1.0).unwrap();
            let got = ndcg_at_k(retrieved.clone(), graded.into_any(), 10).unwrap();
            assert!(got > 0.0);
            // Empty judgments score 1.0 on either shape.
            let empty_list: Bound<'_, PyAny> = PyList::empty(py).into_any();
            assert_eq!(
                ndcg_at_k(retrieved.clone(), empty_list, 10)
                    .unwrap()
                    .to_bits(),
                1.0f64.to_bits()
            );
            let empty_dict: Bound<'_, PyAny> = PyDict::new(py).into_any();
            assert_eq!(
                ndcg_at_k(retrieved, empty_dict, 10).unwrap().to_bits(),
                1.0f64.to_bits()
            );
            // Garbage shapes are ValueErrors, not panics.
            let bad: Bound<'_, PyAny> = 42u32.into_pyobject(py).unwrap().into_any();
            assert!(ndcg_at_k(vec![], bad, 10).is_err());
            let bad_ids: Bound<'_, PyAny> = PyList::new(py, ["nope"]).unwrap().into_any();
            assert!(ndcg_at_k(vec![], bad_ids, 10).is_err());
            let bad_key = PyDict::new(py);
            bad_key.set_item(42u32, 1.0).unwrap();
            assert!(ndcg_at_k(vec![], bad_key.into_any(), 10).is_err());
            let bad_grade = PyDict::new(py);
            bad_grade.set_item(&all[1], "high").unwrap();
            assert!(ndcg_at_k(vec![], bad_grade.into_any(), 10).is_err());
            let bad_uuid = PyDict::new(py);
            bad_uuid.set_item("nope", 1.0).unwrap();
            assert!(ndcg_at_k(vec![], bad_uuid.into_any(), 10).is_err());
            let empty_list: Bound<'_, PyAny> = PyList::empty(py).into_any();
            assert!(ndcg_at_k(vec!["nope".to_owned()], empty_list, 10).is_err());
        });
    }

    #[test]
    fn registration_names_the_metrics() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "ret").unwrap();
            register_ret(&m);
            for name in [
                "recall_at_k",
                "precision_at_k",
                "reciprocal_rank",
                "dcg",
                "ndcg_at_k",
            ] {
                assert!(m.hasattr(name).unwrap(), "missing {name}");
            }
        });
    }
}
