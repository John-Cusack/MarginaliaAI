//! Pure passage-filter translation and keyword-search SQL construction.
//!
//! Ports the pure parts of
//! `adapters/storage/postgres/repositories/passages.py` (`SUPPORTED_FILTERS`,
//! `validate_filters`, `build_candidate_stmt`, `build_keyword_search_sql`,
//! `_name_matches`), `_like_escape` from
//! `adapters/storage/postgres/repositories/document_texts.py`, and the
//! known-config table from `services/search/langconfig.py`.
//!
//! No database access here: the builders return parameterized SQL text with
//! `$N` placeholders plus an ordered [`Param`] list, and the later repos pass
//! executes them via `sqlx`. Extension subqueries arrive as [`ExtensionClause`]
//! values that plugin impls fill in; this module only composes them.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::{Error, Result};

/// Every filter key the passage repository knows how to turn into a WHERE
/// clause, mirroring `SUPPORTED_FILTERS` in `passages.py`.
///
/// A key absent from this set raises rather than being ignored, which is what
/// makes `SearchResult.applied_filters` honest.
pub const SUPPORTED_FILTERS: &[&str] = &[
    "document_types",
    "date_range_start",
    "date_range_end",
    "author_entity_id",
    "recipient_entity_id",
    "mentions_entity_ids",
    "metadata",
    "language",
    "extensions",
    "extension_logic",
];

/// Every regconfig this module vouches for, mirroring `KNOWN_CONFIGS` in
/// `services/search/langconfig.py`.
///
/// Single-sourced from `marginalia_chunk::langconfig` (Phase 2): the table
/// must be identical everywhere SQL interpolation is guarded, and two copies
/// drift. The re-export keeps `filters::KNOWN_CONFIGS` paths resolving.
pub use marginalia_chunk::langconfig::{is_known_config, KNOWN_CONFIGS};

/// An ordered bound-parameter value for [`build_candidate_sql`] output.
///
/// The `$N` placeholder at position `N` (1-based) binds to `params[N - 1]`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Param {
    /// A text bind (`document_type`, dates, `language`, LIKE patterns).
    Text(String),
    /// An entity-id bind (`mentions.entity_id`).
    Uuid(Uuid),
    /// A metadata-containment bind, serialized as JSON by the repos pass.
    Json(serde_json::Value),
}

/// A passage-id subquery contributed by a filter-extension plugin impl.
///
/// `sql` is a `SELECT ... passage-id ...` statement whose placeholders are
/// `$1`-based relative to [`ExtensionClause::params`]; [`build_candidate_sql`]
/// offsets them when inlining so they never collide with the outer params.
/// The later repos pass fills these in from plugin impls.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ExtensionClause {
    /// Passage-id subquery with `$1`-based placeholders into `params`.
    pub sql: String,
    /// Ordered bind values for the relative placeholders.
    pub params: Vec<Param>,
}

/// One requested extension filter: its registered id plus the clause the
/// plugin impl built for the requested value. Order is significant and
/// preserved end to end, matching Python dict insertion order.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ExtensionFilter {
    /// The registered extension id (e.g. `"has_extraction"`).
    pub id: String,
    /// The clause the extension impl built for the requested value.
    pub clause: ExtensionClause,
}

/// How multiple extension filters compose, mirroring `extension_logic`.
///
/// Anything other than `"or"` behaves as `"and"`, exactly like the Python
/// `if extension_logic == "or" ... else` branch.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExtensionLogic {
    /// Each extension contributes a per-clause `IN`, intersecting them.
    #[default]
    And,
    /// Extensions union into a single `IN`, taking the union.
    Or,
}

impl ExtensionLogic {
    /// Parse the raw `extension_logic` filter value.
    pub fn parse(value: &str) -> Self {
        if value == "or" {
            Self::Or
        } else {
            Self::And
        }
    }
}

/// Typed filter input for [`build_candidate_sql`].
///
/// Emptiness mirrors Python truthiness: an empty `document_types` list, an
/// empty/blank date or language string, an empty `mentions_entity_ids` list,
/// or an empty `metadata` object contributes no clause — just as a falsy
/// `filters.get(key)` skips its branch in `build_candidate_stmt`.
///
/// `author_entity_id` / `recipient_entity_id` carry no branch of their own:
/// entity-name resolution needs I/O and happens in the caller, whose resolved
/// names arrive as the `author_names` / `recipient_names` arguments. `None`
/// means "no entity filter" (no clause); `Some(&[])` means "the entity has no
/// names" and matches nothing (`FALSE`), so it never silently matches all.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct CandidateFilters {
    /// `document_types`: `documents.document_type IN (...)`.
    pub document_types: Vec<String>,
    /// `date_range_start`: `documents.created_date_start >= $N::timestamptz`.
    pub date_range_start: Option<String>,
    /// `date_range_end`: `documents.created_date_end <= $N::timestamptz`.
    pub date_range_end: Option<String>,
    /// `mentions_entity_ids`: one `EXISTS`-style `IN` subquery per id.
    pub mentions_entity_ids: Vec<Uuid>,
    /// `metadata`: `CAST(passages.metadata AS JSONB) @> $N`.
    pub metadata: Option<serde_json::Value>,
    /// `language`: `documents.language = $N`.
    pub language: Option<String>,
    /// `extensions`, in request order.
    pub extensions: Vec<ExtensionFilter>,
    /// `extension_logic`, parsed with [`ExtensionLogic::parse`].
    pub extension_logic: ExtensionLogic,
}

impl CandidateFilters {
    /// Requested extension ids in order, for [`validate_filters`].
    pub fn extension_ids(&self) -> Vec<&str> {
        self.extensions.iter().map(|ext| ext.id.as_str()).collect()
    }
}

/// Format a string slice the way Python formats a `list[str]`
/// (`"['a', 'b']"`), so refusal messages are byte-identical to the Python
/// `UnsupportedFilterError` / `UnknownFilterExtension` / `ValueError` texts.
fn py_str_list(items: &[&str]) -> String {
    let inner: Vec<String> = items.iter().map(|s| format!("'{s}'")).collect();
    format!("[{}]", inner.join(", "))
}
/// How filter validation fails, mirroring the two Python exception types
/// (`UnsupportedFilterError` for keys, `UnknownFilterExtension` for ids).
///
/// A dedicated enum rather than strings inside `Error::UnsupportedFilter`,
/// so downstream mapping is total with no catch-all arm: unknown keys and
/// unknown extension ids are different failures, and a `match` over two
/// variants cannot silently gain a third.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FilterValidation {
    /// Filter keys with no repository branch, with the supported list.
    UnknownKeys {
        /// Sorted, deduped offending keys.
        unknown: Vec<String>,
        /// Sorted supported keys.
        supported: Vec<String>,
    },
    /// A requested extension id, with the sorted available list (possibly
    /// empty, which selects the no-registry hint).
    UnknownExtension {
        /// The requested id.
        id: String,
        /// Sorted available ids.
        available: Vec<String>,
    },
}

impl std::fmt::Display for FilterValidation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownKeys { unknown, supported } => {
                let unknown: Vec<&str> = unknown.iter().map(String::as_str).collect();
                let supported: Vec<&str> = supported.iter().map(String::as_str).collect();
                write!(
                    f,
                    "Unsupported filter key(s): {}. Supported keys: {}",
                    py_str_list(&unknown),
                    py_str_list(&supported),
                )
            }
            Self::UnknownExtension { id, available } => {
                let available: Vec<&str> = available.iter().map(String::as_str).collect();
                let hint = if available.is_empty() {
                    "No filter extensions are registered. If this filter comes from a ".to_string()
                        + "pack, check that the pack is enabled."
                } else {
                    format!("Available extensions: {}", py_str_list(&available))
                };
                write!(f, "Unknown filter extension: '{id}'. {hint}")
            }
        }
    }
}

impl std::error::Error for FilterValidation {}

/// Reject filter keys and extension ids that would otherwise be ignored.
///
/// Mirrors `validate_filters` in `passages.py`: unknown keys raise with the
/// unknown list sorted and the supported list sorted; a requested extension
/// id missing from `available_extensions` raises with the available list
/// sorted (or the no-extensions-registered hint when none exist). Empty
/// extension requests are not an error.
pub fn validate_filters(
    filter_keys: &[&str],
    extension_ids: &[&str],
    available_extensions: &[&str],
) -> Result<(), FilterValidation> {
    let mut unknown: Vec<&str> = filter_keys
        .iter()
        .copied()
        .filter(|key| !SUPPORTED_FILTERS.contains(key))
        .collect();
    unknown.sort_unstable();
    unknown.dedup();
    if !unknown.is_empty() {
        let mut supported: Vec<&str> = SUPPORTED_FILTERS.to_vec();
        supported.sort_unstable();
        return Err(FilterValidation::UnknownKeys {
            unknown: unknown.iter().map(ToString::to_string).collect(),
            supported: supported.iter().map(ToString::to_string).collect(),
        });
    }

    for ext_id in extension_ids {
        if !available_extensions.contains(ext_id) {
            let mut available: Vec<&str> = available_extensions.to_vec();
            available.sort_unstable();
            available.dedup();
            return Err(FilterValidation::UnknownExtension {
                id: (*ext_id).to_string(),
                available: available.iter().map(ToString::to_string).collect(),
            });
        }
    }
    Ok(())
}

/// Whether an optional metadata value contributes a clause.
///
/// Mirrors the truthiness of `filters.get("metadata")`: `None` and empty
/// objects/arrays/strings are falsy and contribute nothing.
fn metadata_present(value: &Option<serde_json::Value>) -> bool {
    match value {
        None | Some(serde_json::Value::Null) => false,
        Some(serde_json::Value::Object(map)) => !map.is_empty(),
        Some(serde_json::Value::Array(items)) => !items.is_empty(),
        Some(serde_json::Value::String(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

/// A present non-empty string filter, mirroring truthy `filters.get(key)`.
fn text_present(value: &Option<String>) -> Option<&str> {
    value.as_deref().filter(|s| !s.is_empty())
}

/// Offset the `$1`-based placeholders in an extension subquery by `offset`.
///
/// Extension clauses are built standalone, so their placeholders start at
/// `$1`; inlining appends their params after the outer ones, and the markers
/// move by the outer param count. A `$` not followed by digits is left alone.
fn offset_placeholders(sql: &str, offset: usize) -> String {
    if offset == 0 {
        return sql.to_string();
    }
    let mut out = String::with_capacity(sql.len());
    let mut chars = sql.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '$' {
            let mut digits = String::new();
            while let Some(&next) = chars.peek() {
                if next.is_ascii_digit() {
                    digits.push(next);
                    chars.next();
                } else {
                    break;
                }
            }
            if digits.is_empty() {
                out.push('$');
            } else {
                let shifted: usize = digits.parse().unwrap_or(0) + offset;
                out.push('$');
                out.push_str(&shifted.to_string());
            }
        } else {
            out.push(ch);
        }
    }
    out
}

/// Build the candidate-id `SELECT` as parameterized SQL text.
///
/// Pure port of `build_candidate_stmt` in `passages.py`, returning
/// `(sql, params)` where placeholder `$N` binds `params[N - 1]`. Clause order
/// matches the Python branch order: document types, date start, date end,
/// language, author names, recipient names, one mentions subquery per entity
/// id, metadata containment, then extension composition.
///
/// The documents join is gated exactly like `needs_documents`: any present
/// document-level filter, or entity-name resolution having run (`author_names`
/// / `recipient_names` is `Some`, even when empty).
#[allow(clippy::too_many_lines)]
pub fn build_candidate_sql(
    filters: &CandidateFilters,
    author_names: Option<&[String]>,
    recipient_names: Option<&[String]>,
) -> (String, Vec<Param>) {
    let date_start = text_present(&filters.date_range_start);
    let date_end = text_present(&filters.date_range_end);
    let language = text_present(&filters.language);

    let needs_documents = !filters.document_types.is_empty()
        || date_start.is_some()
        || date_end.is_some()
        || language.is_some()
        || author_names.is_some()
        || recipient_names.is_some();

    let mut sql = String::from("SELECT DISTINCT core.passages.id FROM core.passages");
    if needs_documents {
        sql.push_str(" JOIN core.documents ON core.documents.id = core.passages.document_id");
    }

    let mut clauses: Vec<String> = Vec::new();
    let mut params: Vec<Param> = Vec::new();
    // 1-based placeholder counter kept in step with `params`.
    let next_param = |params: &mut Vec<Param>, param: Param| -> usize {
        params.push(param);
        params.len()
    };

    if !filters.document_types.is_empty() {
        let slots: Vec<String> = filters
            .document_types
            .iter()
            .map(|doc_type| {
                let n = next_param(&mut params, Param::Text(doc_type.clone()));
                format!("${n}")
            })
            .collect();
        clauses.push(format!(
            "core.documents.document_type IN ({})",
            slots.join(", ")
        ));
    }

    // Dates bind as text (the JSON boundary carries strings; the repos pass
    // validates RFC3339 before executing), and Postgres has no
    // `timestamptz >= text` operator — so the cast lives in the builder,
    // next to the comparison, rather than as a statement-local rewrite.
    if let Some(start) = date_start {
        let n = next_param(&mut params, Param::Text(start.to_string()));
        clauses.push(format!(
            "core.documents.created_date_start >= ${n}::timestamptz"
        ));
    }

    if let Some(end) = date_end {
        let n = next_param(&mut params, Param::Text(end.to_string()));
        clauses.push(format!(
            "core.documents.created_date_end <= ${n}::timestamptz"
        ));
    }

    if let Some(lang) = language {
        let n = next_param(&mut params, Param::Text(lang.to_string()));
        clauses.push(format!("core.documents.language = ${n}"));
    }

    // Case-insensitive substring match of the metadata author/recipient
    // against any resolved name; an empty resolved list matches nothing
    // (`FALSE`) rather than silently matching all. Mirrors `_name_matches`.
    let mut name_clause = |key: &str, names: &[String]| {
        if names.is_empty() {
            clauses.push("FALSE".to_string());
            return;
        }
        let alternatives: Vec<String> = names
            .iter()
            .map(|name| {
                let pattern = format!("%{}%", name.to_lowercase());
                let n = next_param(&mut params, Param::Text(pattern));
                format!("LOWER(core.documents.metadata ->> '{key}') LIKE ${n}")
            })
            .collect();
        clauses.push(format!("({})", alternatives.join(" OR ")));
    };
    if let Some(names) = author_names {
        name_clause("author", names);
    }
    if let Some(names) = recipient_names {
        name_clause("recipient", names);
    }

    for entity_id in &filters.mentions_entity_ids {
        let n = next_param(&mut params, Param::Uuid(*entity_id));
        clauses.push(format!(
            "core.passages.id IN (SELECT core.mentions.passage_id \
             FROM core.mentions WHERE core.mentions.entity_id = ${n})"
        ));
    }

    if metadata_present(&filters.metadata) {
        // The column is `json`, not `jsonb`, and a generic-JSON `.contains()`
        // compiles to a string LIKE — which silently matches almost nothing.
        // Cast so this is real containment (`@>`).
        let value = filters.metadata.clone().unwrap_or(serde_json::Value::Null);
        let n = next_param(&mut params, Param::Json(value));
        clauses.push(format!("CAST(core.passages.metadata AS JSONB) @> ${n}"));
    }

    // Extension filters: `validate_filters` has already guaranteed every
    // requested id resolves, so the clauses compose directly.
    if !filters.extensions.is_empty() {
        match filters.extension_logic {
            ExtensionLogic::Or => {
                let mut union: Vec<String> = Vec::new();
                for ext in &filters.extensions {
                    let offset = params.len();
                    params.extend(ext.clause.params.iter().cloned());
                    union.push(offset_placeholders(&ext.clause.sql, offset));
                }
                clauses.push(format!("core.passages.id IN ({})", union.join(" UNION ")));
            }
            ExtensionLogic::And => {
                for ext in &filters.extensions {
                    let offset = params.len();
                    params.extend(ext.clause.params.iter().cloned());
                    let inlined = offset_placeholders(&ext.clause.sql, offset);
                    clauses.push(format!("core.passages.id IN ({inlined})"));
                }
            }
        }
    }

    if !clauses.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&clauses.join(" AND "));
    }
    (sql, params)
}

/// One indexed branch per language config, unioned.
///
/// Ports `build_keyword_search_sql` in `passages.py`. The obvious form —
/// `plainto_tsquery(pf.lang_config, :query)` — is correct and unusable: the
/// tsquery varies per row, so `passage_fts_ts_idx` cannot be used and every
/// search becomes a sequential scan over the FTS table. One branch per
/// distinct config keeps the query constant within each branch, which is what
/// lets the GIN index apply.
///
/// Every config must already pass [`is_known_config`]: configs are
/// interpolated as SQL literals — not bound as parameters — because Postgres
/// requires a literal regconfig in `plainto_tsquery`. Unvalidated configs are
/// refused rather than interpolated.
///
/// The `:query`, `:no_filter`, `:candidate_ids`, and `:k` binders are
/// SQLAlchemy `text()`-style named parameters, kept verbatim for the repos
/// pass, which binds them at execution time.
pub fn build_keyword_search_sql(configs: &[&str]) -> Result<String> {
    if configs.is_empty() {
        return Err(Error::InvalidQuery(
            "build_keyword_search_sql requires at least one config".to_string(),
        ));
    }
    let bad: Vec<&str> = configs
        .iter()
        .copied()
        .filter(|cfg| !is_known_config(cfg))
        .collect();
    if !bad.is_empty() {
        return Err(Error::InvalidQuery(format!(
            "refusing to interpolate unvalidated regconfig(s): {}",
            py_str_list(&bad),
        )));
    }

    let branches: Vec<String> = configs
        .iter()
        .enumerate()
        .map(|(i, cfg)| {
            format!(
                "\n        SELECT pf.passage_id, ts_rank_cd(pf.ts, q{i}.tsq) AS kw_score\
                 \n        FROM core.passage_fts pf, plainto_tsquery('{cfg}', :query) AS q{i}(tsq)\
                 \n        WHERE pf.lang_config = '{cfg}'::regconfig\
                 \n          AND pf.ts @@ q{i}.tsq\
                 \n          AND (:no_filter OR pf.passage_id = ANY(:candidate_ids))\
                 \n        "
            )
        })
        .collect();
    Ok(branches.join("UNION ALL") + "\nORDER BY kw_score DESC\nLIMIT :k")
}

/// Escape LIKE metacharacters so a quote containing `%` or `_` still matches.
///
/// Byte-identical to `_like_escape` in `document_texts.py`: backslash first,
/// then `%`, then `_`. Order matters — escaping `%` first would double-escape
/// the backslash the `%` escape itself introduces.
pub fn like_escape(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn supported_sorted() -> Vec<&'static str> {
        let mut keys = SUPPORTED_FILTERS.to_vec();
        keys.sort_unstable();
        keys
    }

    fn invalid_message(err: &Error) -> &str {
        match err {
            Error::InvalidQuery(msg) => msg,
            other => panic!("expected InvalidQuery, got {other:?}"),
        }
    }

    // --- SUPPORTED_FILTERS shape (mirrors the two reflection tests) ---

    #[test]
    fn supported_filters_cover_every_search_filter_field() {
        // Every field of the Python `SearchFilters` model must have a branch.
        let expected = [
            "document_types",
            "date_range_start",
            "date_range_end",
            "author_entity_id",
            "recipient_entity_id",
            "mentions_entity_ids",
            "metadata",
            "language",
            "extensions",
            "extension_logic",
        ];
        for field in expected {
            assert!(
                SUPPORTED_FILTERS.contains(&field),
                "SearchFilters field with no repository branch: {field}"
            );
        }
    }

    #[test]
    fn supported_filters_have_no_dead_keys() {
        // And nothing in SUPPORTED_FILTERS that SearchFilters cannot express.
        let expressible = [
            "document_types",
            "date_range_start",
            "date_range_end",
            "author_entity_id",
            "recipient_entity_id",
            "mentions_entity_ids",
            "metadata",
            "language",
            "extensions",
            "extension_logic",
        ];
        let dead: Vec<&&str> = SUPPORTED_FILTERS
            .iter()
            .filter(|key| !expressible.contains(key))
            .collect();
        assert!(
            dead.is_empty(),
            "SUPPORTED_FILTERS keys not in SearchFilters: {dead:?}"
        );
        assert_eq!(SUPPORTED_FILTERS.len(), expressible.len());
    }

    // --- validate_filters (mirrors the fail-loud validation tests) ---

    #[test]
    fn unknown_filter_key_raises_with_sorted_lists() {
        let err = validate_filters(&["document_types", "published_after"], &[], &[])
            .expect_err("unknown key must raise");
        let msg = err.to_string();
        assert!(
            msg.contains("Unsupported filter key(s): ['published_after']"),
            "unexpected message: {msg}"
        );
        assert!(
            msg.contains("'document_types'"),
            "unexpected message: {msg}"
        );
    }

    #[test]
    fn unknown_keys_are_sorted_and_deduped() {
        let err = validate_filters(&["zzz", "aaa", "zzz"], &[], &[]).expect_err("must raise");
        let msg = err.to_string();
        assert!(msg.contains("['aaa', 'zzz']"), "unexpected message: {msg}");
        let supported = format!("Supported keys: {}", {
            let items = supported_sorted();
            let refs: Vec<&str> = items.to_vec();
            py_str_list(&refs)
        });
        assert!(msg.contains(&supported), "unexpected message: {msg}");
    }

    #[test]
    fn supported_keys_list_byte_identical_shape() {
        let err = validate_filters(&["nope"], &[], &[]).expect_err("must raise");
        let msg = err.to_string();
        assert_eq!(
            msg,
            "Unsupported filter key(s): ['nope']. Supported keys: \
             ['author_entity_id', 'date_range_end', 'date_range_start', \
             'document_types', 'extension_logic', 'extensions', 'language', \
             'mentions_entity_ids', 'metadata', 'recipient_entity_id']",
        );
    }

    #[test]
    fn known_keys_pass_validation() {
        let keys = SUPPORTED_FILTERS.to_vec();
        validate_filters(&keys, &[], &[]).expect("known keys must pass");
    }

    #[test]
    fn unregistered_extension_raises_with_available() {
        let err = validate_filters(&["extensions"], &["event_date_range"], &["has_extraction"])
            .expect_err("unknown extension must raise");
        let msg = err.to_string();
        assert_eq!(
            msg,
            "Unknown filter extension: 'event_date_range'. \
             Available extensions: ['has_extraction']"
        );
    }

    #[test]
    fn extension_requested_with_no_registry_raises() {
        // The old code silently dropped these — similar_to and extract pass no
        // registry — so a request against an empty registry must raise.
        let err = validate_filters(&["extensions"], &["has_extraction"], &[])
            .expect_err("must raise with no registry");
        let msg = err.to_string();
        assert_eq!(
            msg,
            "Unknown filter extension: 'has_extraction'. No filter extensions are \
             registered. If this filter comes from a pack, check that the pack is enabled."
        );
    }

    #[test]
    fn empty_extensions_are_not_an_error() {
        validate_filters(&["extensions"], &[], &[]).expect("empty extensions must pass");
    }

    // --- build_candidate_sql (mirrors the translation tests) ---

    #[test]
    fn no_filters_produce_no_where_or_join_clause() {
        let (sql, params) = build_candidate_sql(&CandidateFilters::default(), None, None);
        assert_eq!(sql, "SELECT DISTINCT core.passages.id FROM core.passages");
        assert!(!sql.contains("WHERE"));
        assert!(!sql.contains("JOIN"));
        assert!(params.is_empty());
    }

    #[test]
    fn language_narrows_on_documents() {
        let filters = CandidateFilters {
            language: Some("de".to_string()),
            ..CandidateFilters::default()
        };
        let (sql, params) = build_candidate_sql(&filters, None, None);
        assert!(sql.contains("JOIN core.documents"));
        assert!(sql.contains("core.documents.language = $1"));
        assert_eq!(params, vec![Param::Text("de".to_string())]);
    }

    #[test]
    fn document_types_and_language_join_documents_once() {
        let filters = CandidateFilters {
            document_types: vec!["letter".to_string()],
            language: Some("de".to_string()),
            ..CandidateFilters::default()
        };
        let (sql, params) = build_candidate_sql(&filters, None, None);
        assert_eq!(sql.matches("JOIN core.documents").count(), 1);
        assert!(sql.contains("core.documents.document_type IN ($1)"));
        assert!(sql.contains("core.documents.language = $2"));
        assert_eq!(
            params,
            vec![
                Param::Text("letter".to_string()),
                Param::Text("de".to_string())
            ]
        );
    }

    #[test]
    fn date_range_filters_bind_in_order() {
        let filters = CandidateFilters {
            date_range_start: Some("1861-01-01".to_string()),
            date_range_end: Some("1862-01-01".to_string()),
            ..CandidateFilters::default()
        };
        let (sql, params) = build_candidate_sql(&filters, None, None);
        assert!(sql.contains("JOIN core.documents"));
        assert!(sql.contains("core.documents.created_date_start >= $1::timestamptz"));
        assert!(sql.contains("core.documents.created_date_end <= $2::timestamptz"));
        assert_eq!(
            params,
            vec![
                Param::Text("1861-01-01".to_string()),
                Param::Text("1862-01-01".to_string())
            ]
        );
    }

    #[test]
    fn empty_filters_contribute_no_clause() {
        // Falsy values behave as absent, mirroring `filters.get(key)`.
        let filters = CandidateFilters {
            document_types: vec![],
            date_range_start: Some(String::new()),
            language: Some(String::new()),
            metadata: Some(serde_json::json!({})),
            ..CandidateFilters::default()
        };
        let (sql, params) = build_candidate_sql(&filters, None, None);
        assert!(!sql.contains("WHERE"));
        assert!(!sql.contains("JOIN"));
        assert!(params.is_empty());
    }

    #[test]
    fn author_names_match_document_metadata() {
        let names = vec!["Karl Barth".to_string(), "Barth, K.".to_string()];
        let (sql, params) = build_candidate_sql(&CandidateFilters::default(), Some(&names), None);
        assert!(sql.contains("JOIN core.documents"));
        assert!(sql.contains("core.documents.metadata ->> 'author'"));
        // Patterns bind as params (lowered, substring-wrapped), exactly what
        // the Python `compiled_with_values` assertions inline.
        assert_eq!(
            params,
            vec![
                Param::Text("%karl barth%".to_string()),
                Param::Text("%barth, k.%".to_string())
            ]
        );
    }

    #[test]
    fn recipient_names_match_recipient_key() {
        let names = vec!["Thurneysen".to_string()];
        let (sql, params) = build_candidate_sql(&CandidateFilters::default(), None, Some(&names));
        assert!(sql.contains("core.documents.metadata ->> 'recipient'"));
        assert_eq!(params, vec![Param::Text("%thurneysen%".to_string())]);
    }

    #[test]
    fn metadata_filter_uses_jsonb_containment_not_string_like() {
        // The generic JSON type would compile `.contains()` to a LIKE, which
        // matches nothing; the cast makes it real containment (`@>`).
        let filters = CandidateFilters {
            metadata: Some(serde_json::json!({"page": 3})),
            ..CandidateFilters::default()
        };
        let (sql, params) = build_candidate_sql(&filters, None, None);
        assert!(sql.contains("CAST(core.passages.metadata AS JSONB) @> $1"));
        assert!(sql.contains("@>"));
        assert!(!sql.contains("LIKE"));
        assert!(!sql.contains("JOIN"));
        assert_eq!(params, vec![Param::Json(serde_json::json!({"page": 3}))]);
    }

    #[test]
    fn author_entity_with_no_names_matches_nothing() {
        // An entity with no canonical name or aliases must not silently match
        // all: an empty resolved list compiles to FALSE, and the join is still
        // present because resolution ran.
        let empty: Vec<String> = vec![];
        let (sql, params) = build_candidate_sql(&CandidateFilters::default(), Some(&empty), None);
        assert!(sql.contains("JOIN core.documents"));
        assert!(sql.to_lowercase().contains("false"));
        assert!(params.is_empty());
    }

    #[test]
    fn mentions_entity_ids_add_one_subquery_each() {
        let filters = CandidateFilters {
            mentions_entity_ids: vec![Uuid::from_u128(1), Uuid::from_u128(2)],
            ..CandidateFilters::default()
        };
        let (sql, params) = build_candidate_sql(&filters, None, None);
        assert_eq!(sql.matches("FROM core.mentions").count(), 2);
        assert_eq!(sql.matches("core.mentions.entity_id = $").count(), 2);
        assert!(!sql.contains("JOIN core.documents"));
        assert_eq!(
            params,
            vec![
                Param::Uuid(Uuid::from_u128(1)),
                Param::Uuid(Uuid::from_u128(2))
            ]
        );
    }

    fn stub_clause() -> ExtensionClause {
        ExtensionClause {
            sql: "SELECT '00000000-0000-0000-0000-000000000000'::uuid".to_string(),
            params: vec![],
        }
    }

    #[test]
    fn extension_and_logic_intersects() {
        let filters = CandidateFilters {
            extensions: vec![ExtensionFilter {
                id: "has_extraction".to_string(),
                clause: stub_clause(),
            }],
            extension_logic: ExtensionLogic::And,
            ..CandidateFilters::default()
        };
        let (sql, _) = build_candidate_sql(&filters, None, None);
        assert!(sql.contains("IN (SELECT"));
    }

    #[test]
    fn extension_and_logic_emits_one_in_per_clause() {
        // Mirrors the multi-extension AND case: each clause intersects.
        let filters = CandidateFilters {
            extensions: vec![
                ExtensionFilter {
                    id: "ext_a".to_string(),
                    clause: stub_clause(),
                },
                ExtensionFilter {
                    id: "ext_b".to_string(),
                    clause: stub_clause(),
                },
            ],
            extension_logic: ExtensionLogic::parse("and"),
            ..CandidateFilters::default()
        };
        let (sql, _) = build_candidate_sql(&filters, None, None);
        assert_eq!(sql.matches("IN (SELECT").count(), 2);
        assert!(!sql.contains("UNION"));
    }

    #[test]
    fn extension_or_logic_unions() {
        let filters = CandidateFilters {
            extensions: vec![
                ExtensionFilter {
                    id: "ext_a".to_string(),
                    clause: stub_clause(),
                },
                ExtensionFilter {
                    id: "ext_b".to_string(),
                    clause: stub_clause(),
                },
            ],
            extension_logic: ExtensionLogic::parse("or"),
            ..CandidateFilters::default()
        };
        let (sql, _) = build_candidate_sql(&filters, None, None);
        assert!(sql.contains("IN (SELECT"));
        assert_eq!(sql.matches("IN (SELECT").count(), 1);
        assert!(sql.contains("UNION"));
    }

    #[test]
    fn unknown_extension_logic_falls_back_to_and() {
        assert_eq!(ExtensionLogic::parse("xor"), ExtensionLogic::And);
        assert_eq!(ExtensionLogic::parse("or"), ExtensionLogic::Or);
        assert_eq!(ExtensionLogic::default(), ExtensionLogic::And);
    }

    #[test]
    fn extension_placeholders_are_offset_past_outer_params() {
        let filters = CandidateFilters {
            language: Some("de".to_string()),
            extensions: vec![ExtensionFilter {
                id: "ext_a".to_string(),
                clause: ExtensionClause {
                    sql: "SELECT core.passages.id FROM core.passages WHERE \
                          core.passages.text = $1"
                        .to_string(),
                    params: vec![Param::Text("x".to_string())],
                },
            }],
            extension_logic: ExtensionLogic::And,
            ..CandidateFilters::default()
        };
        let (sql, params) = build_candidate_sql(&filters, None, None);
        assert!(sql.contains("core.documents.language = $1"));
        assert!(sql.contains("core.passages.text = $2"));
        assert_eq!(
            params,
            vec![Param::Text("de".to_string()), Param::Text("x".to_string())]
        );
    }

    #[test]
    fn extension_ids_report_in_request_order() {
        // `CandidateFilters::extension_ids` feeds `validate_filters`: order
        // is the request's, not the registry's.
        let filters = CandidateFilters {
            extensions: vec![
                ExtensionFilter {
                    id: "b_ext".to_string(),
                    clause: stub_clause(),
                },
                ExtensionFilter {
                    id: "a_ext".to_string(),
                    clause: stub_clause(),
                },
            ],
            ..CandidateFilters::default()
        };
        assert_eq!(filters.extension_ids(), vec!["b_ext", "a_ext"]);
        assert!(CandidateFilters::default().extension_ids().is_empty());
    }

    #[test]
    fn registered_extension_passes_validation() {
        // A requested id present in the registry is not an error: the loop
        // moves on rather than refusing.
        assert!(validate_filters(&["extensions"], &["my_ext"], &["my_ext"]).is_ok());
    }

    #[test]
    fn metadata_truthiness_mirrors_python_falsiness() {
        // `metadata_present`: None/Null/empty containers are falsy (no
        // clause); non-empty containers and scalars contribute `@>`.
        let falsy: Vec<Option<serde_json::Value>> = vec![
            None,
            Some(serde_json::Value::Null),
            Some(serde_json::Value::Object(serde_json::Map::new())),
            Some(serde_json::Value::Array(Vec::new())),
            Some(serde_json::Value::String(String::new())),
        ];
        for metadata in falsy {
            let filters = CandidateFilters {
                metadata,
                ..CandidateFilters::default()
            };
            let (sql, params) = build_candidate_sql(&filters, None, None);
            assert!(!sql.contains("@>"), "{sql}");
            assert!(params.is_empty());
        }
        let truthy: Vec<serde_json::Value> = vec![
            serde_json::json!({"k": "v"}),
            serde_json::json!(["a"]),
            serde_json::json!("x"),
            serde_json::json!(7),
            serde_json::json!(true),
        ];
        for metadata in truthy {
            let filters = CandidateFilters {
                metadata: Some(metadata),
                ..CandidateFilters::default()
            };
            let (sql, _) = build_candidate_sql(&filters, None, None);
            assert!(sql.contains("@>"), "{sql}");
        }
    }

    #[test]
    fn placeholder_offset_leaves_bare_dollars_alone() {
        // `offset_placeholders`: a `$` with no digits is literal, while
        // `$1` shifts past the outer params.
        let filters = CandidateFilters {
            language: Some("de".to_string()),
            extensions: vec![ExtensionFilter {
                id: "ext_a".to_string(),
                clause: ExtensionClause {
                    sql: "SELECT core.passages.id FROM core.passages \
                          WHERE core.passages.text ~ '$end' \
                          AND core.passages.position > $1"
                        .to_string(),
                    params: vec![Param::Text("1".to_string())],
                },
            }],
            extension_logic: ExtensionLogic::And,
            ..CandidateFilters::default()
        };
        let (sql, _) = build_candidate_sql(&filters, None, None);
        assert!(sql.contains("~ '$end'"), "{sql}");
        assert!(sql.contains("position > $2"), "{sql}");
    }

    #[test]
    #[should_panic(expected = "expected InvalidQuery")]
    fn invalid_message_refuses_other_variants() {
        invalid_message(&Error::UnsupportedFilter("x".to_string()));
    }

    // --- build_keyword_search_sql (mirrors test_langconfig.py keyword cases) ---

    #[test]
    fn single_config_produces_one_indexable_branch() {
        let sql = build_keyword_search_sql(&["german"]).expect("known config");
        assert_eq!(sql.matches("UNION ALL").count(), 0);
        assert!(sql.contains("plainto_tsquery('german', :query)"));
        assert!(sql.contains("pf.lang_config = 'german'::regconfig"));
    }

    #[test]
    fn multiple_configs_are_unioned() {
        let sql =
            build_keyword_search_sql(&["english", "german", "simple"]).expect("known configs");
        assert_eq!(sql.matches("UNION ALL").count(), 2);
        for cfg in ["english", "german", "simple"] {
            assert!(sql.contains(&format!("plainto_tsquery('{cfg}', :query)")));
            assert!(sql.contains(&format!("pf.lang_config = '{cfg}'::regconfig")));
        }
    }

    #[test]
    fn tsquery_is_constant_within_each_branch() {
        // The per-row form is correct and unusable (no GIN index): branches
        // must interpolate the config as a literal instead.
        let sql = build_keyword_search_sql(&["english", "german"]).expect("known configs");
        assert!(!sql.contains("plainto_tsquery(pf.lang_config"));
        assert!(!sql.contains("plainto_tsquery(pf."));
    }

    #[test]
    fn ordering_and_limit_apply_across_the_union() {
        let sql = build_keyword_search_sql(&["english", "german"]).expect("known configs");
        assert!(sql.trim_end().ends_with("LIMIT :k"));
        assert_eq!(sql.matches("ORDER BY kw_score DESC").count(), 1);
    }

    #[test]
    fn candidate_filter_applies_in_every_branch() {
        let sql = build_keyword_search_sql(&["english", "german"]).expect("known configs");
        assert_eq!(
            sql.matches(":no_filter OR pf.passage_id = ANY(:candidate_ids)")
                .count(),
            2
        );
    }

    #[test]
    fn unvalidated_config_is_refused() {
        let err = build_keyword_search_sql(&["english'); DROP TABLE core.passages; --"])
            .expect_err("injection must be refused");
        let msg = invalid_message(&err);
        assert!(
            msg.contains("unvalidated regconfig"),
            "unexpected message: {msg}"
        );
        assert!(
            msg.contains("english'); DROP TABLE core.passages; --"),
            "unexpected message: {msg}"
        );
    }

    #[test]
    fn empty_config_list_is_refused() {
        let err = build_keyword_search_sql(&[]).expect_err("empty must be refused");
        assert_eq!(
            invalid_message(&err),
            "build_keyword_search_sql requires at least one config"
        );
    }

    #[test]
    fn is_known_config_guards_sql_interpolation() {
        assert!(is_known_config("german"));
        assert!(is_known_config("simple"));
        assert!(!is_known_config("klingon"));
        assert!(!is_known_config("english'); DROP TABLE core.passages; --"));
    }

    // --- like_escape (mirrors document_texts._like_escape) ---

    #[test]
    fn like_escape_escapes_metacharacters() {
        assert_eq!(like_escape("100%"), "100\\%");
        assert_eq!(like_escape("a_b"), "a\\_b");
        assert_eq!(like_escape("a\\b"), "a\\\\b");
        assert_eq!(like_escape("plain"), "plain");
        assert_eq!(like_escape(""), "");
    }

    #[test]
    fn like_escape_escapes_backslash_first() {
        // Backslash before %/_: escaping in any other order would double-escape
        // the backslash the later escapes introduce.
        assert_eq!(like_escape("\\%_"), "\\\\\\%\\_");
    }
}
