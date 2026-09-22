//! Work files on disk — parsing the Phase-0 file contract.
//!
//! Python source: `services/works/files.py`. A bad header is a hard error;
//! a bad entry is not (it lands in `entry_errors` for `AUTH_ENTRY_INVALID`).
//!
//! [`WorkFileReader`] does synchronous filesystem reads: the Python is
//! `sync` here too (`parse_work_file`, `list_works`, `read` are plain
//! functions), so there is no purity boundary to split.

use std::fmt;
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use marginalia_types::works_files::{CitationEntry, EntryError, WorkFile, WorkFrontMatter};

use crate::{py_repr_str, py_repr_str_list};

/// A work file cannot be parsed — bad fences, bad YAML, or a bad header.
///
/// Mirrors `WorkFileError`; messages reproduce the Python format strings
/// exactly, except YAML-library and validator internals (human text, not
/// rule ids — the contract is the `Result` shape plus `entry_errors`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkFileError {
    pub message: String,
}

impl WorkFileError {
    fn new(message: String) -> Self {
        Self { message }
    }
}

impl fmt::Display for WorkFileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for WorkFileError {}

impl From<WorkFileError> for marginalia_types::Error {
    fn from(error: WorkFileError) -> Self {
        Self::Validation(error.message)
    }
}

/// Files that live in the works directory but are not works.
static NON_WORK_NAMES: LazyLock<std::collections::HashSet<&'static str>> =
    LazyLock::new(|| ["README.md", "_TEMPLATE.md"].into_iter().collect());

/// A footnote definition line: `[^c1]: ...` at line start. Definitions are
/// the renderer's output, not markers, so they are excluded before markers
/// are read.
pub static DEFINITION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?m)^\[\^c\d+\]:[^\n]*\n?").expect("definition regex"));

/// Every `[^cN]` marker left in the body once definitions are removed.
pub static HANDLE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[\^(c\d+)\]").expect("handle regex"));

/// Top-level front-matter keys. Anything else is a typo for one of them, and
/// a typo that parses is worse than a header that refuses.
static HEADER_KEYS: &[&str] = &[
    "work",
    "title",
    "type",
    "status",
    "created",
    "claims",
    "citations",
];

/// Remove rendered footnote definitions, leaving prose and markers.
pub fn strip_definition_lines(body: &str) -> String {
    DEFINITION_RE.replace_all(body, "").into_owned()
}

/// Parse one file's text. A bad header is a hard error; a bad entry is not.
///
/// `work_path` is the forward-slash path relative to the works directory
/// (containment is checked by [`WorkFileReader::read`], which owns paths).
pub fn parse_work_file_text(work_path: &str, text: &str) -> Result<WorkFile, WorkFileError> {
    if !text.starts_with("---\n") {
        return Err(WorkFileError::new(format!(
            "{work_path} does not start with a front-matter fence (`---`)"
        )));
    }
    // Python searches `text.find("\n---\n", 4)`: the closing fence must start
    // on its own line, so an inline `---` inside the YAML never matches.
    let end = text[4..]
        .find("\n---\n")
        .map(|index| index + 4)
        .ok_or_else(|| {
            WorkFileError::new(format!("{work_path} has no closing front-matter fence"))
        })?;
    let yaml_block = &text[4..end];
    let body = &text[end + "\n---\n".len()..];

    // `serde_yaml` has no timestamp type, so `created: 2026-09-04` arrives as
    // a string where PyYAML yields a `datetime.date`; both feed header
    // validation below, which is what coerces to `NaiveDate`.
    let yaml_value: serde_yaml::Value = serde_yaml::from_str(yaml_block).map_err(|err| {
        // serde's message text is its own; the contract is the hard error.
        WorkFileError::new(format!("{work_path} has invalid YAML front matter: {err}"))
    })?;
    let raw = yaml_to_json(&yaml_value);
    let raw_map = match raw {
        Value::Object(map) => map,
        // `yaml.safe_load("")` is `None`, which is not a mapping either.
        _ => {
            return Err(WorkFileError::new(format!(
                "{work_path} front matter must be a mapping"
            )));
        }
    };

    let (front_matter, entry_errors) = validate_header(work_path, raw_map)?;
    let sha = format!("{:x}", Sha256::digest(yaml_block.as_bytes()));
    let markers = HANDLE_RE
        .captures_iter(&strip_definition_lines(body))
        .map(|capture| capture[1].to_owned())
        .collect();
    Ok(WorkFile {
        work_path: work_path.to_owned(),
        front_matter,
        front_matter_sha: sha,
        body: body.to_owned(),
        markers,
        entry_errors,
    })
}

/// `serde_yaml::Value` to `serde_json::Value`: the header/entry models
/// deserialize from JSON, so the YAML tree is converted once here.
fn yaml_to_json(value: &serde_yaml::Value) -> Value {
    match value {
        serde_yaml::Value::Null => Value::Null,
        serde_yaml::Value::Bool(flag) => Value::Bool(*flag),
        serde_yaml::Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                Value::from(int)
            } else if let Some(uint) = number.as_u64() {
                Value::from(uint)
            } else {
                // Infallible: serde_yaml numbers are always i64-, u64-, or
                // f64-backed, so a non-integer converts to f64 exactly here.
                Value::from(number.as_f64().expect("YAML number is f64-representable"))
            }
        }
        serde_yaml::Value::String(text) => Value::String(text.clone()),
        serde_yaml::Value::Sequence(items) => {
            Value::Array(items.iter().map(yaml_to_json).collect())
        }
        serde_yaml::Value::Mapping(entries) => {
            let mut map = Map::new();
            for (key, item) in entries {
                // Front-matter keys are strings; a non-string key cannot name
                // a header field, so it renders as YAML debug text and fails
                // validation downstream like any unknown key.
                let name = match key {
                    serde_yaml::Value::String(text) => text.clone(),
                    other => format!("{other:?}"),
                };
                map.insert(name, yaml_to_json(item));
            }
            Value::Object(map)
        }
        serde_yaml::Value::Tagged(tagged) => yaml_to_json(&tagged.value),
    }
}

fn validate_header(
    work_path: &str,
    raw: Map<String, Value>,
) -> Result<(WorkFrontMatter, Vec<EntryError>), WorkFileError> {
    let mut unknown: Vec<String> = raw
        .keys()
        .filter(|key| !HEADER_KEYS.contains(&key.as_str()))
        .cloned()
        .collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(WorkFileError::new(unknown_keys_message(
            work_path, &unknown,
        )));
    }
    let entries = match raw.get("citations") {
        None => Vec::new(),
        Some(Value::Array(items)) => items.clone(),
        Some(_) => {
            return Err(WorkFileError::new(format!(
                "{work_path} front matter `citations` must be a list"
            )));
        }
    };
    // Mirror `WorkFrontMatter(**header_raw, citations=[])`: only known
    // non-entry keys ride along, and entries validate one by one below.
    let mut header_value = Map::new();
    for key in HEADER_KEYS.iter().filter(|key| **key != "citations") {
        if let Some(value) = raw.get(*key) {
            header_value.insert((*key).to_owned(), value.clone());
        }
    }
    coerce_created_date(&mut header_value);
    header_value.insert("citations".to_owned(), Value::Array(Vec::new()));
    // The serde message text is its own (pydantic's is not portable); the
    // contract is the hard error carrying it.
    let mut header: WorkFrontMatter = serde_json::from_value(Value::Object(header_value))
        .map_err(|err| WorkFileError::new(invalid_header_message(work_path, &err.to_string())))?;

    let mut valid = Vec::new();
    let mut errors = Vec::new();
    for item in &entries {
        match item {
            Value::Object(map) => match build_entry(map) {
                Ok(entry) => valid.push(entry),
                Err(error) => errors.push(error),
            },
            other => errors.push(EntryError {
                citation_id: None,
                message: format!("citation entry must be a mapping: {}", py_repr_value(other)),
            }),
        }
    }
    header.citations = valid;
    Ok((header, errors))
}

/// `created` arrives as a YAML string but may carry a time of day
/// (`created: 2026-09-04T10:00:00`); pydantic coerces datetimes to dates, so
/// a datetime string is truncated to its date here before `NaiveDate`
/// deserialization. Anything unparseable is left alone to fail as a header
/// error, exactly like a non-date in Python.
fn coerce_created_date(header_value: &mut Map<String, Value>) {
    let text = match header_value.get("created") {
        Some(Value::String(text)) => text.clone(),
        _ => return,
    };
    if chrono::NaiveDate::parse_from_str(&text, "%Y-%m-%d").is_ok() {
        return;
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ] {
        if let Ok(datetime) = chrono::NaiveDateTime::parse_from_str(&text, format) {
            header_value.insert(
                "created".to_owned(),
                Value::String(datetime.date().format("%Y-%m-%d").to_string()),
            );
            return;
        }
    }
    if let Ok(datetime) = chrono::DateTime::parse_from_rfc3339(&text) {
        header_value.insert(
            "created".to_owned(),
            Value::String(datetime.date_naive().format("%Y-%m-%d").to_string()),
        );
    }
}

fn build_entry(item: &Map<String, Value>) -> Result<CitationEntry, EntryError> {
    let citation_id = item.get("id").and_then(Value::as_str).map(str::to_owned);
    // Field/validator failures are serde/validation text (pydantic's wording
    // is not portable); the contract is the `EntryError` shape carrying it.
    let entry: CitationEntry =
        serde_json::from_value(Value::Object(item.clone())).map_err(|err| EntryError {
            citation_id: citation_id.clone(),
            message: err.to_string(),
        })?;
    entry.validate().map_err(|err| EntryError {
        citation_id: citation_id.clone(),
        message: err.to_string(),
    })?;
    Ok(entry)
}

/// Python `repr` of a YAML/JSON scalar or collection, for the non-mapping
/// entry message (`f"... {item!r}"`). Strings ride on [`py_repr_str`];
/// `True`/`False`/`None` spell as Python does. No test pins this text —
/// the contract is `citation_id: None` — but the shape stays recognizable.
fn py_repr_value(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => py_repr_str(text),
        Value::Array(items) => {
            let inner = items
                .iter()
                .map(py_repr_value)
                .collect::<Vec<_>>()
                .join(", ");
            format!("[{inner}]")
        }
        Value::Object(map) => {
            let inner = map
                .iter()
                .map(|(key, item)| format!("{}: {}", py_repr_str(key), py_repr_value(item)))
                .collect::<Vec<_>>()
                .join(", ");
            format!("{{{inner}}}")
        }
    }
}

/// Works on disk, addressed by path relative to the works directory.
pub struct WorkFileReader {
    works_dir: PathBuf,
}

impl WorkFileReader {
    pub fn new(works_dir: PathBuf) -> Self {
        Self {
            works_dir: canonicalize_lossy(&works_dir),
        }
    }

    pub fn works_dir(&self) -> &Path {
        &self.works_dir
    }

    /// Every work file, as forward-slash paths relative to the works dir.
    ///
    /// `README.md`, `_TEMPLATE.md`, and anything starting with `_` are the
    /// contract itself, not works.
    pub fn list_works(&self) -> Vec<String> {
        let mut found = Vec::new();
        collect_markdown(&self.works_dir, &self.works_dir, &mut found);
        // `sorted` on `Path`s compares component-wise, not byte-wise
        // (`a/b.md` sorts before `a-b.md`): split before comparing.
        found.sort_by(|left, right| left.split('/').cmp(right.split('/')));
        found
    }

    pub fn read(&self, work_path: &str) -> Result<WorkFile, WorkFileError> {
        let requested = Path::new(work_path);
        let joined = if requested.is_absolute() {
            requested.to_path_buf()
        } else {
            self.works_dir.join(requested)
        };
        let resolved = canonicalize_lossy(&joined);
        let relative = resolved.strip_prefix(&self.works_dir).map_err(|_| {
            WorkFileError::new(format!(
                "{resolved} is outside the works directory",
                resolved = resolved.display()
            ))
        })?;
        // `resolved` is absolute and dot-free, so stripping the works dir
        // leaves only `Normal` components: read straight, never filter.
        let normalized = relative
            .components()
            .map(|component| component.as_os_str().to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join("/");
        let text = std::fs::read_to_string(&resolved)
            .map_err(|err| WorkFileError::new(format!("Cannot read {normalized}: {err}")))?;
        parse_work_file_text(&normalized, &text)
    }
}

/// Recursively collect `*.md` files minus the contract files, as posix
/// paths relative to `root`. Missing directories read as empty.
fn collect_markdown(root: &Path, dir: &Path, found: &mut Vec<String>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut dirs = Vec::new();
    let mut files = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            dirs.push(path);
        } else if path.extension().is_some_and(|ext| ext == "md") {
            files.push(path);
        }
    }
    for path in files {
        let name = path
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default();
        if NON_WORK_NAMES.contains(name.as_str()) || name.starts_with('_') {
            continue;
        }
        // Infallible: `path` joins `dir` under `root`, so the root prefix
        // always strips and every component below is `Normal` — read
        // straight, never filter. Ancestors stay: `sub/inner.md` lists whole.
        let relative = path
            .strip_prefix(root)
            .expect("read_dir entries live under the works root");
        found.push(
            relative
                .components()
                .map(|component| component.as_os_str().to_string_lossy().into_owned())
                .collect::<Vec<_>>()
                .join("/"),
        );
    }
    for dir in dirs {
        collect_markdown(root, &dir, found);
    }
}

/// `Path::canonicalize` needs the target to exist; work files resolve
/// lexically first (like Python's non-strict `Path.resolve()`), so the
/// containment check runs before any read.
fn canonicalize_lossy(path: &Path) -> PathBuf {
    if let Ok(resolved) = path.canonicalize() {
        return resolved;
    }
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        // `current_dir` fails only when the process outlives its cwd; the
        // default empty path then joins back to `path`, keeping resolution
        // total so the lexical walk below still runs.
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let mut out = PathBuf::new();
    for component in absolute.components() {
        // `absolute` is always absolute (see above), and `Components` over
        // an absolute path never yields `CurDir` — interior `.` segments
        // are normalized away — so only parents and real parts remain.
        match component {
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Format the unknown-keys error exactly: Python interpolates the sorted
/// `list[str]`, whose `repr` is `['a', 'b']` single-quoted.
pub(crate) fn unknown_keys_message(work_path: &str, unknown: &[String]) -> String {
    format!(
        "{work_path} has unknown front-matter keys: {}",
        py_repr_str_list(unknown)
    )
}

/// Human-text stub for validator failures (pydantic internals are not
/// portable; the rule id downstream is the contract).
pub(crate) fn invalid_header_message(work_path: &str, detail: &str) -> String {
    format!("{work_path} has an invalid front-matter header: {detail}")
}

#[cfg(test)]
mod tests {
    use super::*;

    const DOC_ID: &str = "11111111-1111-1111-1111-111111111111";

    /// Mirrors `GOOD` in `tests/unit/works/test_work_files.py`.
    fn good_text() -> String {
        format!(
            "---\n\
             work: W-001\n\
             title: \"A dabaris fragment\"\n\
             type: essay\n\
             status: draft\n\
             created: 2026-09-04\n\
             claims: [TEST-001]\n\
             citations:\n\
             \x20 - id: c1\n\
             \x20   document_id: {DOC_ID}\n\
             \x20   char_start: 34\n\
             \x20   char_end: 62\n\
             \x20   quoted_text: \"The prophets pair two words.\"\n\
             \x20   intent: quotation\n\
             \x20   edition_key: DABAR_2026\n\
             \x20   locator: {{page: 1}}\n\
             \x20 - id: c2\n\
             \x20   document_id: {DOC_ID}\n\
             \x20   char_start: 63\n\
             \x20   char_end: 117\n\
             \x20   quoted_text: \"He requires justice\"\n\
             \x20   intent: background\n\
             ---\n\
             \n\
             ## Notes\n\
             \n\
             Reading [^c1] closely, then [^c2] for context, and [^c1] again.\n\
             \n\
             [^c1]: rendered elsewhere, never parsed as a marker.\n\
             [^c9]: a dangling definition is not a marker either.\n"
        )
    }

    fn write(dir: &std::path::Path, name: &str, text: &str) -> PathBuf {
        let path = dir.join(name);
        std::fs::write(&path, text).expect("fixture write");
        path
    }

    #[test]
    fn test_parses_to_the_expected_models() {
        let dir = tempfile::tempdir().expect("tempdir");
        write(dir.path(), "essay.md", &good_text());

        let work = WorkFileReader::new(dir.path().to_path_buf())
            .read("essay.md")
            .expect("good file parses");

        assert_eq!(work.work_path, "essay.md");
        assert_eq!(work.front_matter.work, "W-001");
        assert_eq!(work.front_matter.title, "A dabaris fragment");
        assert_eq!(
            work.front_matter.work_type,
            marginalia_types::works_files::WorkType::Essay
        );
        assert_eq!(
            work.front_matter.status,
            marginalia_types::works_files::WorkFileStatus::Draft
        );
        assert_eq!(work.front_matter.claims, vec!["TEST-001".to_owned()]);
        let ids = work
            .front_matter
            .citations
            .iter()
            .map(|entry| entry.id.clone())
            .collect::<Vec<_>>();
        assert_eq!(ids, vec!["c1".to_owned(), "c2".to_owned()]);
        assert_eq!(
            work.front_matter.citations[0].locator.get("page"),
            Some(&Value::from(1))
        );
        assert!(work.entry_errors.is_empty());
    }

    #[test]
    fn test_missing_claims_default_to_empty() {
        // Every header key but `claims` rides the `if let Some` arm in
        // `validate_header`; an absent `claims` takes the false edge and
        // serde fills the default.
        let text = good_text().replace("claims: [TEST-001]\n", "");
        let work = parse_work_file_text("essay.md", &text).expect("claims default");
        assert_eq!(work.front_matter.claims, Vec::<String>::new());
        assert_eq!(work.front_matter.citations.len(), 2);
    }

    #[test]
    fn test_unparseable_created_is_a_header_error() {
        // A string `created` that is no date, datetime, or RFC 3339 value
        // survives coercion untouched and fails header deserialization.
        let text = good_text().replace("created: 2026-09-04\n", "created: yesterday\n");
        let err = parse_work_file_text("essay.md", &text).expect_err("bad date fails");
        assert!(
            err.message.contains("has an invalid front-matter header"),
            "{}",
            err.message
        );
    }

    #[test]
    fn test_sha_is_stable_and_covers_only_the_yaml_block() {
        let dir = tempfile::tempdir().expect("tempdir");
        let text = good_text();
        write(dir.path(), "essay.md", &text);
        let reader = WorkFileReader::new(dir.path().to_path_buf());

        let first = reader.read("essay.md").expect("parses");
        let second = reader.read("essay.md").expect("parses");
        assert_eq!(first.front_matter_sha, second.front_matter_sha);

        let raw_block = text.split("\n---\n").next().expect("fence")["---\n".len()..].to_owned();
        assert_eq!(
            first.front_matter_sha,
            format!("{:x}", Sha256::digest(raw_block.as_bytes()))
        );
    }

    #[test]
    fn test_markers_are_found_in_order_and_definitions_excluded() {
        let work = parse_work_file_text("essay.md", &good_text()).expect("parses");
        assert_eq!(
            work.markers,
            vec!["c1".to_owned(), "c2".to_owned(), "c1".to_owned()]
        );
    }

    #[test]
    fn test_reader_lists_works_but_not_the_contract() {
        let dir = tempfile::tempdir().expect("tempdir");
        let text = good_text();
        write(dir.path(), "essay.md", &text);
        std::fs::write(dir.path().join("README.md"), "# contract\n").expect("write");
        std::fs::write(dir.path().join("_TEMPLATE.md"), "---\n").expect("write");
        std::fs::write(dir.path().join("_draft.md"), "---\n").expect("write");

        assert_eq!(
            WorkFileReader::new(dir.path().to_path_buf()).list_works(),
            vec!["essay.md".to_owned()]
        );
    }

    #[test]
    fn test_nested_works_list_with_ancestors_in_path_order() {
        // Python sorts `Path`s component-wise (`a/b.md` before `a-b.md`)
        // and keeps ancestors (`sub/inner.md`, not `inner.md`).
        let dir = tempfile::tempdir().expect("tempdir");
        let text = good_text();
        write(dir.path(), "a-b.md", &text);
        write(dir.path(), "essay.md", &text);
        std::fs::create_dir(dir.path().join("a")).expect("mkdir");
        write(&dir.path().join("a"), "b.md", &text);

        assert_eq!(
            WorkFileReader::new(dir.path().to_path_buf()).list_works(),
            vec![
                "a/b.md".to_owned(),
                "a-b.md".to_owned(),
                "essay.md".to_owned()
            ]
        );
    }

    #[test]
    fn test_one_invalid_entry_yields_one_error_and_the_other_parses() {
        let bad = good_text()
            .replace("    intent: background", "    intent: frobnicate")
            .replace(
                "    quoted_text: \"He requires justice\"",
                "    quoted_text: \"\"",
            );
        let work = parse_work_file_text("essay.md", &bad).expect("bad entry is not fatal");

        let ids = work
            .front_matter
            .citations
            .iter()
            .map(|entry| entry.id.clone())
            .collect::<Vec<_>>();
        assert_eq!(ids, vec!["c1".to_owned()]);
        assert_eq!(work.entry_errors.len(), 1);
        assert_eq!(work.entry_errors[0].citation_id.as_deref(), Some("c2"));
    }

    #[test]
    fn test_each_malformed_field_is_an_entry_error() {
        let good_entry = format!(
            "  - id: c2\n\
             \x20   document_id: {DOC_ID}\n\
             \x20   char_start: 63\n\
             \x20   char_end: 117\n\
             \x20   quoted_text: \"He requires justice\"\n\
             \x20   intent: background\n"
        );
        let base_entry = format!(
            "  - id: c2\n\
             \x20   document_id: {DOC_ID}\n\
             \x20   char_start: 34\n\
             \x20   char_end: 62\n\
             \x20   quoted_text: \"The prophets pair two words.\"\n\
             \x20   intent: quotation\n"
        );
        for (old, new) in [
            ("  - id: c2\n", "  - id: x1\n"),
            ("    char_start: 34\n", "    char_start: -1\n"),
            ("    char_end: 62\n", "    char_end: 34\n"),
            (
                "    document_id: 11111111-1111-1111-1111-111111111111\n",
                "    document_id: not-a-uuid\n",
            ),
        ] {
            let bad_entry = base_entry.replace(old, new);
            let text = good_text().replace(&good_entry, &bad_entry);
            let work = parse_work_file_text("essay.md", &text).expect("entry error is not fatal");
            let ids = work
                .front_matter
                .citations
                .iter()
                .map(|entry| entry.id.clone())
                .collect::<Vec<_>>();
            assert_eq!(ids, vec!["c1".to_owned()], "case {old:?} -> {new:?}");
            assert_eq!(work.entry_errors.len(), 1, "case {old:?} -> {new:?}");
        }
    }

    #[test]
    fn test_a_bad_header_is_a_hard_error() {
        let texts = vec![
            "no fence at all\n".to_owned(),
            "---\nwork: W-001\n".to_owned(),
            "---\n- just\n- a\n- list\n---\nbody\n".to_owned(),
            good_text().replace("type: essay", "type: pamphlet"),
            good_text().replace("status: draft", "status: someday"),
            good_text().replace("work: W-001", "work: W-001\ntitel: typo"),
            good_text().replace("citations:\n", "citations: {}\n"),
        ];
        for text in texts {
            assert!(parse_work_file_text("essay.md", &text).is_err(), "{text:?}");
        }
    }

    #[test]
    fn test_unknown_key_message_names_sorted_keys() {
        let text = good_text().replace("work: W-001", "work: W-001\ntitel: typo\nzzz: 1");
        let err = parse_work_file_text("essay.md", &text).expect_err("unknown keys fail");
        assert_eq!(
            err.message,
            "essay.md has unknown front-matter keys: ['titel', 'zzz']"
        );
    }

    #[test]
    fn test_a_file_outside_the_works_dir_is_refused() {
        let dir = tempfile::tempdir().expect("tempdir");
        let elsewhere = write(dir.path(), "elsewhere.md", &good_text());
        let reader = WorkFileReader::new(dir.path().join("works"));

        let err = reader
            .read(elsewhere.to_str().expect("utf-8"))
            .expect_err("outside file refused");
        assert!(
            err.message.ends_with(" is outside the works directory"),
            "{err}"
        );
    }

    #[test]
    fn test_missing_file_reports_cannot_read() {
        let dir = tempfile::tempdir().expect("tempdir");
        let reader = WorkFileReader::new(dir.path().to_path_buf());

        let err = reader.read("gone.md").expect_err("missing file fails");
        assert!(err.message.starts_with("Cannot read gone.md: "), "{err}");
    }

    #[test]
    fn test_non_mapping_entry_is_an_error_without_id() {
        let text = good_text().replace(
            "    locator: {page: 1}\n",
            "    locator: {page: 1}\n  - just-a-string\n",
        );
        let work = parse_work_file_text("essay.md", &text).expect("non-mapping entry is not fatal");
        assert_eq!(work.front_matter.citations.len(), 2);
        assert_eq!(work.entry_errors.len(), 1);
        assert_eq!(work.entry_errors[0].citation_id, None);
    }

    #[test]
    fn test_created_datetime_string_becomes_a_date() {
        let text = good_text().replace("created: 2026-09-04", "created: 2026-09-04T10:00:00");
        let work = parse_work_file_text("essay.md", &text).expect("datetime created parses");
        assert_eq!(
            work.front_matter.created,
            chrono::NaiveDate::from_ymd_opt(2026, 9, 4).expect("valid date")
        );
    }

    #[test]
    fn test_error_display_shows_the_message() {
        let err =
            parse_work_file_text("essay.md", "no fence at all\n").expect_err("bad file fails");
        assert_eq!(err.to_string(), err.message);
        assert!(!err.message.is_empty());
    }

    #[test]
    fn test_error_converts_to_a_validation_error() {
        let err =
            parse_work_file_text("essay.md", "no fence at all\n").expect_err("bad file fails");
        let converted = marginalia_types::Error::from(err.clone());
        assert_eq!(
            converted.to_string(),
            format!("data validation failed: {}", err.message)
        );
    }

    #[test]
    fn test_reader_reports_its_works_dir() {
        let dir = tempfile::tempdir().expect("tempdir");
        let reader = WorkFileReader::new(dir.path().to_path_buf());
        assert_eq!(
            reader.works_dir(),
            dir.path().canonicalize().expect("tempdir exists")
        );
    }

    #[test]
    fn test_relative_missing_dir_resolves_lexically_and_lists_empty() {
        // A relative works dir for nothing on disk exercises the lexical
        // (non-canonical) resolve plus the missing-directory list path.
        let reader = WorkFileReader::new(std::path::PathBuf::from("missing-relative-dir"));
        assert!(reader.works_dir().ends_with("missing-relative-dir"));
        assert_eq!(reader.list_works(), Vec::<String>::new());
    }

    #[test]
    fn test_list_descends_into_subdirectories() {
        let dir = tempfile::tempdir().expect("tempdir");
        write(dir.path(), "essay.md", &good_text());
        let sub = dir.path().join("sub");
        std::fs::create_dir(&sub).expect("mkdir");
        std::fs::write(sub.join("notes.txt"), "not markdown\n").expect("write");

        assert_eq!(
            WorkFileReader::new(dir.path().to_path_buf()).list_works(),
            vec!["essay.md".to_owned()]
        );
    }

    #[test]
    fn test_dot_segments_resolve_lexically_before_the_read() {
        let dir = tempfile::tempdir().expect("tempdir");
        write(dir.path(), "essay.md", &good_text());
        let reader = WorkFileReader::new(dir.path().to_path_buf());

        let err = reader.read("./gone.md").expect_err("missing file fails");
        assert!(err.message.starts_with("Cannot read gone.md: "), "{err}");
        let err = reader
            .read("sub/../gone.md")
            .expect_err("missing file fails");
        assert!(err.message.starts_with("Cannot read gone.md: "), "{err}");

        // Python re-derives the posix path after resolving, so dot-dot
        // spellings parse (and report) as the plain relative path.
        let work = reader.read("sub/../essay.md").expect("dot-dot normalizes");
        assert_eq!(work.work_path, "essay.md");
        assert_eq!(work.front_matter.citations.len(), 2);
    }

    #[test]
    fn test_yaml_scalars_convert_before_header_validation() {
        // A null header value converts, then fails as a header error.
        let text = good_text().replace("title: \"A dabaris fragment\"", "title: ~");
        assert!(
            parse_work_file_text("essay.md", &text).is_err(),
            "null title fails"
        );
        // Boolean, huge-integer, and float values convert, then fail as
        // unknown keys (the message text is `py_repr` shaped; only the
        // hard-error shape is the contract here).
        for value in ["true", "18446744073709551615", "1.5"] {
            let text = good_text().replace("work: W-001", &format!("work: W-001\nzzz: {value}"));
            assert!(
                parse_work_file_text("essay.md", &text).is_err(),
                "value {value} fails"
            );
        }
        // A tagged scalar unwraps to its inner value and parses.
        let text = good_text().replace("title: \"A dabaris fragment\"", "title: !frag dabaris");
        let work = parse_work_file_text("essay.md", &text).expect("tagged title parses");
        assert_eq!(work.front_matter.title, "dabaris");
        // A non-string mapping key renders as YAML debug text and fails as
        // an unknown key.
        let text = good_text().replace("work: W-001", "work: W-001\n1: oops");
        assert!(
            parse_work_file_text("essay.md", &text).is_err(),
            "numeric key fails"
        );
    }

    #[test]
    fn test_absent_citations_parse_as_empty() {
        let head = "---\n\
            work: W-001\n\
            title: \"A fragment\"\n\
            type: essay\n\
            status: draft\n\
            created: 2026-09-04\n\
            claims: []\n\
            ---\n\nbody without entries\n";
        let work = parse_work_file_text("essay.md", head).expect("citations may be absent");
        assert!(work.front_matter.citations.is_empty());
        assert!(work.entry_errors.is_empty());
    }

    #[test]
    fn test_non_list_citations_are_a_hard_error() {
        let head = "---\n\
            work: W-001\n\
            title: \"A fragment\"\n\
            type: essay\n\
            status: draft\n\
            created: 2026-09-04\n\
            claims: []\n\
            citations: {}\n\
            ---\n\nbody\n";
        let err = parse_work_file_text("essay.md", head).expect_err("mapping citations fail");
        assert_eq!(
            err.message,
            "essay.md front matter `citations` must be a list"
        );
    }

    #[test]
    fn test_non_string_created_is_left_to_fail_as_a_header_error() {
        let text = good_text().replace("created: 2026-09-04", "created: 2026");
        assert!(
            parse_work_file_text("essay.md", &text).is_err(),
            "numeric created fails"
        );
    }

    #[test]
    fn test_created_datetime_spellings_all_become_a_date() {
        let expected = chrono::NaiveDate::from_ymd_opt(2026, 9, 4).expect("valid date");
        for spelling in [
            "2026-09-04 10:00:00",
            "2026-09-04T10:00:00.123",
            "2026-09-04T10:00:00Z",
        ] {
            let text = good_text().replace("created: 2026-09-04", &format!("created: {spelling}"));
            let work = parse_work_file_text("essay.md", &text).expect("datetime parses");
            assert_eq!(work.front_matter.created, expected, "spelling {spelling}");
        }
    }

    #[test]
    fn test_every_non_mapping_entry_shape_is_an_error_without_id() {
        // Each YAML shape reaches the non-mapping message (whose exact
        // `repr` text is not the contract — only the `None` id and the
        // shared prefix are asserted).
        for entry in [
            "  - ~\n",
            "  - true\n",
            "  - false\n",
            "  - 42\n",
            "  - [1, \"two\"]\n",
            "  - [1, {a: b}]\n",
        ] {
            let text = good_text().replace(
                "    locator: {page: 1}\n",
                &format!("    locator: {{page: 1}}\n{entry}"),
            );
            let work =
                parse_work_file_text("essay.md", &text).expect("non-mapping entry is not fatal");
            assert_eq!(work.front_matter.citations.len(), 2, "entry {entry:?}");
            assert_eq!(work.entry_errors.len(), 1, "entry {entry:?}");
            let error = &work.entry_errors[0];
            assert_eq!(error.citation_id, None, "entry {entry:?}");
            assert!(
                error
                    .message
                    .starts_with("citation entry must be a mapping: "),
                "entry {entry:?}: {}",
                error.message
            );
        }
    }
}
