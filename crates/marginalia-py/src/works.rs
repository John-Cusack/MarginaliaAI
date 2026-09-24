//! Works bindings over `marginalia-works`: `dominant_century` only.
//!
//! The one works seam that cleared the accelerator benchmark's gate (~23x
//! on a letters volume). Content hashing, citation markers, and fuzzy-date
//! parsing and scanning were cut back to pure Python: they lost to the
//! crossing cost or saved microseconds.

use pyo3::prelude::*;

/// The century a text is written about, from the years it spells out.
/// Mirrors `services/text/dates.py::dominant_century` exactly.
#[pyfunction]
fn dominant_century(text: &str) -> Option<i64> {
    marginalia_works::dates::dominant_century(text)
}

pub fn works_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "works").expect("module name is a valid literal");
    register_works(&m);
    m
}

pub fn register_works(m: &Bound<'_, PyModule>) {
    m.add_function(
        wrap_pyfunction!(dominant_century, m).expect("function name is a unique literal"),
    )
    .expect("module attribute assignment");
}

#[cfg(test)]
mod tests {
    use super::{dominant_century, register_works};
    use pyo3::prelude::*;

    #[test]
    fn registration_names_only_dominant_century() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "works").unwrap();
            register_works(&m);
            assert!(m.hasattr("dominant_century").unwrap());
            for name in [
                "compute_content_hash",
                "find_markers",
                "format_marker",
                "parse_fuzzy_date",
                "scan_dates",
            ] {
                assert!(!m.hasattr(name).unwrap(), "{name} was cut back to Python");
            }
        });
    }

    #[test]
    fn century_matches_crate() {
        for text in [
            "1801 1802 1803 1804 1805 1901",
            "too few years 1801",
            "",
            "1861 1862 1863 1911 1912 1913",
        ] {
            assert_eq!(
                dominant_century(text),
                marginalia_works::dates::dominant_century(text)
            );
        }
        assert_eq!(
            dominant_century("1801 1802 1803 1804 1805 1901"),
            Some(1800)
        );
        assert_eq!(dominant_century("too few years 1801"), None);
    }
}
