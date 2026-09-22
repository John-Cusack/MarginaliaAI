//! `pg_config` / `is_known_config` bindings over `marginalia-chunk::langconfig`.
//!
//! The whole module is a pure table lookup (plus the `simple` fallback), so
//! the seam is signature-identical with no crossing concerns: strings in,
//! strings and bools out. Locales, case, and surrounding whitespace behave
//! exactly as upstream (proven by the crate's differential); `None` and
//! anything unrecognised answer `simple`.

use marginalia_chunk::langconfig as chunk_langconfig;
use pyo3::prelude::*;
use pyo3::types::PyFrozenSet;

/// Map an ISO 639-1 code (or a full locale like `de-CH`) to a regconfig.
///
/// Anything unrecognised — including `None` — maps to `simple`.
/// Mirrors `services/search/langconfig.py::pg_config` exactly.
#[pyfunction]
#[pyo3(signature = (iso = None))]
fn pg_config(iso: Option<&str>) -> String {
    chunk_langconfig::pg_config(iso).to_owned()
}

/// Whether `config` is a regconfig this module vouches for.
///
/// Mirrors `services/search/langconfig.py::is_known_config` exactly.
#[pyfunction]
fn is_known_config(config: &str) -> bool {
    chunk_langconfig::is_known_config(config)
}

pub fn register_langconfig(m: &Bound<'_, PyModule>) {
    m.add_function(wrap_pyfunction!(pg_config, m).expect("function name is a unique literal"))
        .expect("module attribute assignment");
    m.add_function(
        wrap_pyfunction!(is_known_config, m).expect("function name is a unique literal"),
    )
    .expect("module attribute assignment");
    m.add("DEFAULT_CONFIG", chunk_langconfig::DEFAULT_CONFIG)
        .expect("module attribute assignment");
    let known =
        PyFrozenSet::new(m.py(), chunk_langconfig::KNOWN_CONFIGS).expect("static table builds");
    m.add("KNOWN_CONFIGS", known)
        .expect("module attribute assignment");
}

#[cfg(test)]
mod tests {
    use super::{is_known_config, pg_config, register_langconfig};
    use pyo3::prelude::*;
    use pyo3::types::PyFrozenSet;

    #[test]
    fn langconfig_matches_python_on_every_shape() {
        assert_eq!(pg_config(None), "simple");
        assert_eq!(pg_config(Some("")), "simple");
        assert_eq!(pg_config(Some("  ")), "simple");
        assert_eq!(pg_config(Some("en")), "english");
        assert_eq!(pg_config(Some("de-CH")), "german");
        assert_eq!(pg_config(Some(" EL ")), "greek");
        assert_eq!(pg_config(Some("xx")), "simple");
        assert_eq!(pg_config(Some("e")), "simple");
        assert!(is_known_config("german"));
        assert!(is_known_config("simple"));
        assert!(!is_known_config("english; DROP TABLE x"));
        assert!(!is_known_config(""));
    }

    #[test]
    fn registration_names_the_langconfig() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "chunk").unwrap();
            register_langconfig(&m);
            assert!(m.hasattr("pg_config").unwrap());
            assert!(m.hasattr("is_known_config").unwrap());
            assert_eq!(
                m.getattr("DEFAULT_CONFIG")
                    .unwrap()
                    .extract::<String>()
                    .unwrap(),
                "simple"
            );
            let known: Bound<'_, PyFrozenSet> =
                m.getattr("KNOWN_CONFIGS").unwrap().extract().unwrap();
            assert_eq!(known.len(), 28);
            let via_module: String = m
                .getattr("pg_config")
                .unwrap()
                .call1(("fr",))
                .unwrap()
                .extract()
                .unwrap();
            assert_eq!(via_module, "french");
            let none_default: String = m
                .getattr("pg_config")
                .unwrap()
                .call0()
                .unwrap()
                .extract()
                .unwrap();
            assert_eq!(none_default, "simple");
        });
    }
}
