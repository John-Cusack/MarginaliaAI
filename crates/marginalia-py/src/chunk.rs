//! `marginalia_rs.chunk` — the chunker bindings kept by the accelerator
//! benchmark's gate (`chunk_prose`, `chunk_structural`; see `chunkers`).
//!
//! Fusion, windows, langconfig, and the fixed/whole chunkers were cut back
//! to pure Python: they lost to the crossing cost or saved microseconds.

use pyo3::prelude::*;

pub fn chunk_module(py: Python<'_>) -> Bound<'_, PyModule> {
    let m = PyModule::new(py, "chunk").expect("module name is a valid literal");
    super::chunkers::register_chunkers(&m);
    m
}
