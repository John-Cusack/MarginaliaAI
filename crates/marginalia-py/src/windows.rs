//! `choose_window` / `build_window` bindings over `marginalia-chunk::windows`.
//!
//! Crossing contract (all pinned by tests):
//! - Nodes cross as `DocumentNode` JSON (`model_dump_json`), deserialized
//!   into the real structs — no fabricated fields. `metadata` floats ride
//!   along unread and unreturned, so the ≥17-digit decimal finding cannot
//!   surface; a metadata float battery pins identical output either way.
//! - Spans cross as `(start, end)` integer pairs. Negative coordinates clamp
//!   to 0 at the `usize` boundary (the typed domain is non-negative —
//!   storage validates it — so this fires only on malformed input, where
//!   the crate's own `Span = usize` rule already applies).
//! - Results cross back as plain tuples; the Python adapter reattaches the
//!   original `Span` / `WindowPlan` / node objects, so identity holds.
//! - Budgets cross as `i64` untouched (negative budgets are in the proven
//!   differential).

use marginalia_chunk::windows as chunk_windows;
use marginalia_text::anchoring::Span;
use marginalia_types::nodes::DocumentNode;
use marginalia_types::passages::WindowSource;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use uuid::Uuid;
/// Clamp a coordinate into the non-negative `usize` domain.
fn clamp_usize(value: i64) -> usize {
    value.max(0) as usize
}

/// Parse one `DocumentNode` from the adapter's JSON dump.
///
/// # Errors
///
/// Returns `ValueError` when the dump is not a `DocumentNode`.
fn parse_node(json: &str) -> PyResult<DocumentNode> {
    serde_json::from_str(json).map_err(|err| {
        PyValueError::new_err(format!(
            "marginalia_rs.chunk expects DocumentNode JSON, got error: {err}"
        ))
    })
}

/// Parse nodes for one call.
fn parse_chain(chain_json: &[String]) -> PyResult<Vec<DocumentNode>> {
    chain_json.iter().map(|json| parse_node(json)).collect()
}

fn source_str(source: WindowSource) -> &'static str {
    match source {
        WindowSource::Node => "node",
        WindowSource::NodeWindow => "node_window",
        WindowSource::DocumentWindow => "document_window",
        WindowSource::Passage => "passage",
    }
}

/// A decided window: `(span_start, span_end, source, bound_id_hex | None)`.
type ChosenWindow = (usize, usize, String, Option<String>);

/// A built window: `(text, char_start, char_end, source, node_id_hex | None,
/// breadcrumb, approx_tokens)`.
type BuiltWindow = (String, i64, i64, String, Option<String>, Vec<String>, i64);

/// Decide the span to read around `passage`.
///
/// Mirrors `services/search/windows.py::choose_window`: `passage` is
/// `(start, end)` or `None`, `ancestors_json` holds `DocumentNode` dumps.
/// Answers `(span_start, span_end, source, bound_id_hex | None)` or `None`.
///
/// # Errors
///
/// Returns `ValueError` when a node dump is not a `DocumentNode`.
#[pyfunction]
#[pyo3(signature = (passage, ancestors_json, *, budget_chars, min_chars))]
fn choose_window(
    passage: Option<(i64, i64)>,
    ancestors_json: Vec<String>,
    budget_chars: i64,
    min_chars: i64,
) -> PyResult<Option<ChosenWindow>> {
    let passage = passage.map(|(start, end)| Span {
        start: clamp_usize(start),
        end: clamp_usize(end),
    });
    let ancestors = parse_chain(&ancestors_json)?;
    let plan = chunk_windows::choose_window(passage, &ancestors, budget_chars, min_chars);
    Ok(plan.map(|p| {
        (
            p.span.start,
            p.span.end,
            source_str(p.source).to_owned(),
            p.node_id.map(|id| id.to_string()),
        )
    }))
}

/// Parse a plan source back into the enum.
///
/// # Errors
///
/// Returns `ValueError` on an unknown source. The adapter only ever passes
/// through a validated `WindowSource` literal, so this fires solely on
/// hand-built calls.
fn parse_source(source: &str) -> PyResult<WindowSource> {
    match source {
        "node" => Ok(WindowSource::Node),
        "node_window" => Ok(WindowSource::NodeWindow),
        "document_window" => Ok(WindowSource::DocumentWindow),
        "passage" => Ok(WindowSource::Passage),
        other => Err(PyValueError::new_err(format!(
            "marginalia_rs.chunk.build_window expects a WindowSource, got {other:?}"
        ))),
    }
}

/// Parse a node id back into a `Uuid`.
///
/// # Errors
///
/// Returns `ValueError` when the id is not a UUID.
fn parse_id(id: &str) -> PyResult<Uuid> {
    Uuid::parse_str(id).map_err(|_| {
        PyValueError::new_err(format!(
            "marginalia_rs.chunk expects UUID node ids, got {id:?}"
        ))
    })
}

/// Build the expanded read from the fetched slice.
///
/// Mirrors `services/search/windows.py::_build_window`: `plan` is
/// `((span_start, span_end), source, node_id_hex | None)`. Answers
/// `(text, char_start, char_end, source, node_id_hex | None, breadcrumb,
/// approx_tokens)` or `None`.
///
/// # Errors
///
/// Returns `ValueError` when the plan source, node id, or chain dumps are
/// malformed.
#[pyfunction]
#[pyo3(signature = (passage, plan, chain_json, raw))]
fn build_window(
    passage: Option<(i64, i64)>,
    plan: ((i64, i64), String, Option<String>),
    chain_json: Vec<String>,
    raw: Option<String>,
) -> PyResult<Option<BuiltWindow>> {
    let span = passage.map(|(start, end)| Span {
        start: clamp_usize(start),
        end: clamp_usize(end),
    });
    let ((plan_start, plan_end), source, node_id) = plan;
    let plan = chunk_windows::WindowPlan {
        span: Span {
            start: clamp_usize(plan_start),
            end: clamp_usize(plan_end),
        },
        source: parse_source(&source)?,
        node_id: node_id.map(|id| parse_id(&id)).transpose()?,
    };
    let chain = parse_chain(&chain_json)?;
    let window = chunk_windows::build_window(span, &plan, &chain, raw.as_deref());
    Ok(window.map(|w| {
        (
            w.text,
            w.char_start,
            w.char_end,
            source_str(w.source).to_owned(),
            w.node_id.map(|id| id.to_string()),
            w.breadcrumb,
            w.approx_tokens,
        )
    }))
}

pub fn register_windows(m: &Bound<'_, PyModule>) {
    m.add_function(wrap_pyfunction!(choose_window, m).expect("function name is a unique literal"))
        .expect("module attribute assignment");
    m.add_function(wrap_pyfunction!(build_window, m).expect("function name is a unique literal"))
        .expect("module attribute assignment");
}

#[cfg(test)]
mod tests {
    use super::{build_window, choose_window, parse_id, parse_source, register_windows};
    use marginalia_chunk::windows as chunk_windows;
    use marginalia_text::anchoring::Span;
    use marginalia_types::nodes::DocumentNode;
    use pyo3::prelude::*;

    const DOC: &str = "12345678-1234-5678-1234-567812345600";

    fn node_json(id_seed: u8, start: i64, end: i64, title: Option<&str>) -> String {
        serde_json::json!({
            "id": format!("12345678-1234-5678-1234-5678123456{id_seed:02}"),
            "document_id": DOC,
            "parent_id": null,
            "path": format!("r.n{id_seed}"),
            "depth": id_seed as i64,
            "position": 0,
            "node_type": "section",
            "title": title,
            "char_start": start,
            "char_end": end,
            "metadata": {"float": 1.0 / 65.0},
            "created_at": "2026-09-22T12:00:00Z",
        })
        .to_string()
    }

    fn parse_nodes(json: &[String]) -> Vec<DocumentNode> {
        json.iter()
            .map(|j| serde_json::from_str(j).unwrap())
            .collect()
    }

    #[test]
    fn choose_matches_crate_across_sources() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|_py| {
            // Narrow entry under a wide root: climbs to a node_window.
            let chain = vec![
                node_json(0, 0, 200_000, Some("Root")),
                node_json(1, 1_200, 1_268, Some("Entry")),
            ];
            let out = choose_window(Some((1_190, 1_290)), chain.clone(), 6_000, 800).unwrap();
            let nodes = parse_nodes(&chain);
            let expected = chunk_windows::choose_window(
                Some(Span {
                    start: 1_190,
                    end: 1_290,
                }),
                &nodes,
                6_000,
                800,
            );
            assert_eq!(
                out,
                expected.map(|p| (
                    p.span.start,
                    p.span.end,
                    super::source_str(p.source).to_owned(),
                    p.node_id.map(|id| id.to_string()),
                ))
            );
            // No passage span: no window.
            assert!(choose_window(None, chain.clone(), 6_000, 800)
                .unwrap()
                .is_none());
            // No ancestors: a document window with no bound.
            let (s, e, src, bound) = choose_window(Some((10, 20)), vec![], 6_000, 800)
                .unwrap()
                .unwrap();
            assert_eq!(
                (s, e, src.as_str(), bound),
                (0, 6_000, "document_window", None)
            );
            // A node that already holds the chunk: the node itself.
            let tight = vec![node_json(2, 100, 900, Some("Tight"))];
            let (_, _, src, _) = choose_window(Some((200, 300)), tight, 6_000, 100)
                .unwrap()
                .unwrap();
            assert_eq!(src, "node");
            // A chunk identical to its node reads as the passage source once the
            // floor returns exactly the chunk.
            let same = vec![node_json(3, 200, 300, None)];
            let (_, _, src, _) = choose_window(Some((200, 300)), same, 50, 10)
                .unwrap()
                .unwrap();
            assert_eq!(src, "passage");
            // Negative coordinates clamp rather than wrap.
            let neg = choose_window(Some((-50, 100)), vec![], 500, 10)
                .unwrap()
                .unwrap();
            assert_eq!((neg.0, neg.1), (0, 500));
            let _ = (
                parse_id("12345678-1234-5678-1234-567812345601").unwrap(),
                parse_source("node").unwrap(),
            );
        });
    }

    #[test]
    fn choose_rejects_garbage_nodes() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|_py| {
            assert!(choose_window(Some((1, 2)), vec!["[not json".to_owned()], 100, 10).is_err());
            assert!(choose_window(Some((1, 2)), vec!["{\"id\": 1}".to_owned()], 100, 10).is_err());
        });
    }

    #[test]
    fn build_matches_crate_on_fetched_slices() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|_py| {
            let chain = vec![
                node_json(0, 0, 1_000, Some("Root")),
                node_json(1, 100, 900, None),
            ];
            let nodes = parse_nodes(&chain);
            let plan = chunk_windows::choose_window(
                Some(Span {
                    start: 200,
                    end: 300,
                }),
                &nodes,
                6_000,
                100,
            )
            .unwrap();
            let raw = format!("{}pad text here{}", " ".repeat(210), " ".repeat(50));
            for source in ["node", "node_window", "document_window", "passage"] {
                let out = build_window(
                    Some((200, 300)),
                    (
                        (plan.span.start as i64, plan.span.end as i64),
                        source.to_owned(),
                        plan.node_id.map(|id| id.to_string()),
                    ),
                    chain.clone(),
                    Some(raw.clone()),
                )
                .unwrap()
                .unwrap();
                assert_eq!(out.3, source);
            }
            let out = build_window(
                Some((200, 300)),
                (
                    (plan.span.start as i64, plan.span.end as i64),
                    "node".to_owned(),
                    plan.node_id.map(|id| id.to_string()),
                ),
                chain.clone(),
                Some(raw.clone()),
            )
            .unwrap()
            .unwrap();
            let expected = chunk_windows::build_window(
                Some(Span {
                    start: 200,
                    end: 300,
                }),
                &plan,
                &nodes,
                Some(&raw),
            )
            .unwrap();
            assert_eq!(out.0, expected.text);
            assert_eq!((out.1, out.2), (expected.char_start, expected.char_end));
            assert_eq!(out.3, "node");
            assert_eq!(out.4, expected.node_id.map(|id| id.to_string()));
            assert_eq!(out.5, expected.breadcrumb);
            assert_eq!(out.6, expected.approx_tokens);
            // No canonical text, empty slice, and whitespace-only slices: no window.
            let plan_arg = (
                (plan.span.start as i64, plan.span.end as i64),
                "node".to_owned(),
                plan.node_id.map(|id| id.to_string()),
            );
            assert!(
                build_window(Some((200, 300)), plan_arg.clone(), chain.clone(), None)
                    .unwrap()
                    .is_none()
            );
            assert!(build_window(
                Some((200, 300)),
                plan_arg.clone(),
                chain.clone(),
                Some(String::new())
            )
            .unwrap()
            .is_none());
            assert!(build_window(
                Some((200, 300)),
                plan_arg.clone(),
                chain.clone(),
                Some("   ".to_owned())
            )
            .unwrap()
            .is_none());
            // A spanless passage still builds (no floor to re-apply).
            let nosuch = build_window(None, plan_arg, chain, Some(raw))
                .unwrap()
                .unwrap();
            assert!(!nosuch.0.is_empty());
        });
    }

    #[test]
    fn build_rejects_malformed_plans() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|_py| {
            let chain = vec![node_json(0, 0, 1_000, Some("Root"))];
            let good = ((0, 100), "node".to_owned(), None);
            assert!(build_window(
                Some((0, 10)),
                ((0, 100), "shelf".to_owned(), None),
                chain.clone(),
                Some("x".repeat(120))
            )
            .is_err());
            assert!(build_window(
                Some((0, 10)),
                ((0, 100), "node".to_owned(), Some("nope".to_owned())),
                chain.clone(),
                Some("x".repeat(120)),
            )
            .is_err());
            assert!(build_window(
                Some((0, 10)),
                good,
                vec!["[bad".to_owned()],
                Some("x".repeat(120))
            )
            .is_err());
        });
    }

    #[test]
    fn registration_names_the_windows() {
        pyo3::prepare_freethreaded_python();
        Python::with_gil(|py| {
            let m = PyModule::new(py, "chunk").unwrap();
            register_windows(&m);
            assert!(m.hasattr("choose_window").unwrap());
            assert!(m.hasattr("build_window").unwrap());
        });
    }
}
