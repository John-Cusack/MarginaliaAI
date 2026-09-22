//! `RE_` settings resolution, defaults, and validation rules as pure logic.
//!
//! Ports the portable core of `config/settings.py`: [`ENV_PREFIX`],
//! [`SECRET_FIELDS`], the scalar [`default_str`] table, the literal-option
//! validators, the derived [`resolve_plugins_dir`] /
//! [`resolve_inference_base_url`] / [`require_works_dir`] rules, env-file
//! discovery ([`find_env_file`]), key parsing ([`parse_env_file_keys`]),
//! source attribution ([`attribute_source`]), secret masking
//! ([`mask_value`]), and load precedence ([`resolve_precedence`]).
//!
//! The `pydantic-settings` binding itself (`Settings`, `load_settings`)
//! stays Python: it is the framework, and no Rust consumer exists yet.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::errors::{Error, Result};

/// Prefix every setting's environment variable carries.
pub const ENV_PREFIX: &str = "RE_";

/// Fields whose values must never be printed or logged.
pub const SECRET_FIELDS: &[&str] = &[
    "anthropic_api_key",
    "openai_compatible_api_key",
    "embedding_api_key",
];

/// Every setting, in `Settings` declaration order — the order
/// `describe_settings` reports in.
pub const SETTING_FIELDS: &[&str] = &[
    "db_url",
    "llm_provider",
    "anthropic_api_key",
    "openai_compatible_base_url",
    "openai_compatible_api_key",
    "default_llm_model",
    "llm_budget_usd",
    "llm_budget_window_days",
    "inference_base_url",
    "embedding_provider",
    "embedding_model",
    "embedding_dim",
    "embedding_base_url",
    "embedding_timeout",
    "embedding_api_key",
    "reranker_provider",
    "reranker_model",
    "reranker_timeout",
    "data_dir",
    "plugins_dir",
    "works_dir",
    "works_policy",
    "ingest_concurrency",
    "embedding_batch_size",
    "docling_device",
    "docling_max_workers",
    "docling_pages_per_task",
    "extraction_concurrency",
    "extraction_retry_on_validation",
    "default_language",
    "search_window_max_tokens",
    "search_window_min_tokens",
    "hnsw_ef_search",
    "log_level",
    "log_format",
];

/// The environment variable carrying a setting: `RE_` + uppercase name.
/// Field names are ASCII, so this matches Python's `str.upper()` exactly.
pub fn env_var_name(field: &str) -> String {
    format!("{ENV_PREFIX}{}", field.to_ascii_uppercase())
}

/// Python `str()` of a field's default, for scalar-defaulted fields.
///
/// `None` covers both `None` defaults (unset scalars) and non-scalar
/// defaults (paths, `works_policy`) — use [`SETTING_FIELDS`] for membership,
/// and [`default_data_dir`] for the one computed path default.
pub fn default_str(field: &str) -> Option<&'static str> {
    match field {
        "db_url" => Some("postgresql+asyncpg://re_dev:re_dev_pass@localhost:5435/research_engine"),
        "llm_provider" => Some("anthropic"),
        "default_llm_model" => Some("claude-sonnet-4-5-20250929"),
        "llm_budget_window_days" => Some("30"),
        "embedding_provider" => Some("local_bge"),
        "embedding_model" => Some("BAAI/bge-m3"),
        "embedding_dim" => Some("1024"),
        "embedding_timeout" => Some("120.0"),
        "reranker_provider" => Some("local_bge"),
        "reranker_model" => Some("BAAI/bge-reranker-v2-m3"),
        "reranker_timeout" => Some("30.0"),
        "ingest_concurrency" => Some("4"),
        "embedding_batch_size" => Some("32"),
        "docling_device" => Some("cpu"),
        "docling_pages_per_task" => Some("50"),
        "extraction_concurrency" => Some("8"),
        "extraction_retry_on_validation" => Some("True"),
        "search_window_max_tokens" => Some("1500"),
        "search_window_min_tokens" => Some("200"),
        "hnsw_ef_search" => Some("100"),
        "log_level" => Some("INFO"),
        "log_format" => Some("pretty"),
        _ => None,
    }
}

/// Allowed values for the `Literal` fields; anything else is a config error
/// on the Python side. Unknown fields carry no rule.
pub const LLM_PROVIDERS: &[&str] = &["anthropic", "openai_compatible"];
/// `local_bge` is the off switch that ignores a set base URL; `remote_api`
/// fails without one; `auto` offloads when a host answers.
pub const EMBEDDING_PROVIDERS: &[&str] = &["local_bge", "remote_api", "auto"];
/// Same modes as embedding, plus `none` to skip reranking entirely.
pub const RERANKER_PROVIDERS: &[&str] = &["local_bge", "remote_api", "auto", "none"];
/// Accelerator for the single-process Docling path.
pub const DOCLING_DEVICES: &[&str] = &["cpu", "auto", "cuda"];
pub const LOG_FORMATS: &[&str] = &["pretty", "json"];

/// Whether a value is accepted for a `Literal`-typed setting. Fields without
/// a rule accept anything — validation lives in the binding, this is the rule
/// table it enforces.
pub fn valid_option(field: &str, value: &str) -> bool {
    match field {
        "llm_provider" => LLM_PROVIDERS.contains(&value),
        "embedding_provider" => EMBEDDING_PROVIDERS.contains(&value),
        "reranker_provider" => RERANKER_PROVIDERS.contains(&value),
        "docling_device" => DOCLING_DEVICES.contains(&value),
        "log_format" => LOG_FORMATS.contains(&value),
        _ => true,
    }
}

/// Default `data_dir`: `~/.research-engine` via `$HOME`, mirroring
/// `Path.home()`. `None` when no home is discoverable.
pub fn default_data_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|home| Path::new(&home).join(".research-engine"))
}

/// `resolved_plugins_dir`: explicit wins, else under the data dir.
pub fn resolve_plugins_dir(plugins_dir: Option<&str>, data_dir: Option<&str>) -> Option<String> {
    if let Some(explicit) = plugins_dir.filter(|s| !s.is_empty()) {
        return Some(explicit.to_owned());
    }
    data_dir
        .filter(|s| !s.is_empty())
        .map(|data| format!("{data}/plugins"))
}

/// `resolved_inference_base_url`: the GPU host address, honouring the
/// deprecated embedding-only name. Empty counts as unset, like Python `or`.
pub fn resolve_inference_base_url(
    inference_base_url: Option<&str>,
    embedding_base_url: Option<&str>,
) -> Option<String> {
    inference_base_url
        .filter(|s| !s.is_empty())
        .or_else(|| embedding_base_url.filter(|s| !s.is_empty()))
        .map(str::to_owned)
}

/// `resolved_works_dir`: the configured works directory, or a loud refusal —
/// the work tools answer `works_not_configured` rather than an empty result.
pub fn require_works_dir(works_dir: Option<&str>) -> Result<String> {
    match works_dir.filter(|s| !s.is_empty()) {
        Some(dir) => Ok(dir.to_owned()),
        None => Err(Error::Config(
            "RE_WORKS_DIR is not set, so no work file can be read. \
             Point it at the folder holding works/*.md."
                .to_owned(),
        )),
    }
}

/// Keys defined in an env file. Values are never read here — presence is what
/// attribution needs. Tolerates comments, blanks, `export` prefixes, and
/// malformed lines exactly like the Python loop.
pub fn parse_env_file_keys(text: &str) -> HashSet<String> {
    let mut keys = HashSet::new();
    for raw in text.lines() {
        let line = raw
            .trim()
            .strip_prefix("export ")
            .unwrap_or(raw.trim())
            .trim();
        if line.is_empty() || line.starts_with('#') || !line.contains('=') {
            continue;
        }
        // Key case matches Python's `str.upper()`; keys are ASCII in practice.
        keys.insert(line.split('=').next().unwrap_or("").trim().to_uppercase());
    }
    keys
}

/// Which env file was used, and how it was chosen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvFileResolution {
    pub path: Option<PathBuf>,
    pub reason: String,
}

impl EnvFileResolution {
    /// The resolution points at a file that exists.
    pub fn exists(&self) -> bool {
        self.path.as_ref().is_some_and(|p| p.is_file())
    }
}

/// Expand a leading `~` against the home directory, mirroring
/// `Path.expanduser()` for the forms that matter here (`~` and `~/...`).
/// `~otheruser/...` is left unexpanded — resolving other users' homes needs
/// the account database, and an explicit env-file path naming one is absurd;
/// accepted residual divergence, same class as the Phase 4 findings.
/// `home` is a parameter (not read here) so every arm is unit-testable
/// without touching the process environment.
fn expand_user(path: &str, home: Option<&std::ffi::OsStr>) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/").or_else(|| path.strip_prefix("~\\")) {
        if let Some(dir) = home {
            return Path::new(dir).join(rest);
        }
    } else if path == "~" {
        if let Some(dir) = home {
            return PathBuf::from(dir);
        }
    }
    PathBuf::from(path)
}

/// Locate the `.env` file, independent of where the process was started.
///
/// Resolution order: `RE_ENV_FILE` when given (explicit always wins, and a
/// bad path is loud — it falls back with a "missing" reason rather than
/// silently reading a found file), else the nearest `.env` walking up from
/// `start`, stopping at the directory holding `pyproject.toml` so the search
/// cannot wander into `$HOME`.
pub fn find_env_file(start: &Path, env_file_override: Option<&str>) -> EnvFileResolution {
    if let Some(override_path) = env_file_override.filter(|s| !s.is_empty()) {
        let path = expand_user(override_path, std::env::var_os("HOME").as_deref());
        if !path.is_file() {
            return EnvFileResolution {
                path: None,
                reason: format!("{ENV_PREFIX}ENV_FILE={override_path} (missing)"),
            };
        }
        // `Path.resolve()`: absolute, symlinks resolved; the file exists, so
        // `canonicalize` agrees with it.
        let resolved = path.canonicalize().unwrap_or(path);
        return EnvFileResolution {
            path: Some(resolved),
            reason: format!("{ENV_PREFIX}ENV_FILE"),
        };
    }
    let origin = start.to_path_buf();
    let mut directory: Option<&Path> = Some(&origin);
    while let Some(dir) = directory {
        if dir.join(".env").is_file() {
            return EnvFileResolution {
                path: Some(dir.join(".env")),
                reason: format!("nearest .env above {}", origin.display()),
            };
        }
        if dir.join("pyproject.toml").is_file() {
            break;
        }
        directory = dir.parent();
    }
    EnvFileResolution {
        path: None,
        reason: format!("no .env found above {}", origin.display()),
    }
}

/// Where a single setting's value came from, mirroring pydantic-settings
/// precedence: explicit override, then environment, then env file, then default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SettingSource {
    Override,
    Environment,
    EnvFile,
    Default,
}

impl SettingSource {
    /// The `source` string `describe_settings` reports.
    pub fn as_str(&self) -> &'static str {
        match self {
            SettingSource::Override => "override",
            SettingSource::Environment => "environment",
            SettingSource::EnvFile => "env file",
            SettingSource::Default => "default",
        }
    }
}

/// Attribute one setting's value: override beats environment beats env file
/// beats default. A value equal to the default still attributes to the file
/// when the file names it — the exact confusion this reporting exists to end.
pub fn attribute_source(
    field: &str,
    overrides: &HashSet<String>,
    environ: &HashMap<String, String>,
    file_keys: &HashSet<String>,
) -> SettingSource {
    if overrides.contains(field) {
        SettingSource::Override
    } else if environ.contains_key(&env_var_name(field)) {
        SettingSource::Environment
    } else if file_keys.contains(&env_var_name(field)) {
        SettingSource::EnvFile
    } else {
        SettingSource::Default
    }
}

/// Render one setting's value for the report. Secrets never print: `SET`
/// when present (even empty), `unset` when absent. Unset non-secrets render
/// `unset`, not an empty string.
pub fn mask_value(field: &str, raw: Option<&str>) -> String {
    if SECRET_FIELDS.contains(&field) {
        if raw.is_some() {
            "SET".to_owned()
        } else {
            "unset".to_owned()
        }
    } else {
        raw.map(str::to_owned).unwrap_or_else(|| "unset".to_owned())
    }
}

/// Resolve one value by load precedence, returning its source and value.
/// The merge itself lives in the Python binding; this is the rule it follows.
pub fn resolve_precedence(
    field: &str,
    override_value: Option<&str>,
    environ: &HashMap<String, String>,
    file_values: &HashMap<String, String>,
    default: &str,
) -> (SettingSource, String) {
    if let Some(value) = override_value {
        return (SettingSource::Override, value.to_owned());
    }
    let env_var = env_var_name(field);
    if let Some(value) = environ.get(&env_var) {
        return (SettingSource::Environment, value.clone());
    }
    if let Some(value) = file_values.get(&env_var) {
        return (SettingSource::EnvFile, value.clone());
    }
    (SettingSource::Default, default.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::{HashMap, HashSet};

    // Mirrors tests/unit/test_config.py + test_config_resolution.py
    // case-for-case, against the pure rules (the binding stays Python).

    #[test]
    fn default_settings_match() {
        assert_eq!(default_str("llm_provider"), Some("anthropic"));
        assert_eq!(default_str("embedding_provider"), Some("local_bge"));
        assert_eq!(default_str("ingest_concurrency"), Some("4"));
        assert_eq!(default_str("log_level"), Some("INFO"));
    }

    #[test]
    fn full_defaults_table_is_pinned() {
        let table: &[(&str, Option<&str>)] = &[
            (
                "db_url",
                Some("postgresql+asyncpg://re_dev:re_dev_pass@localhost:5435/research_engine"),
            ),
            ("llm_provider", Some("anthropic")),
            ("anthropic_api_key", None),
            ("openai_compatible_base_url", None),
            ("openai_compatible_api_key", None),
            ("default_llm_model", Some("claude-sonnet-4-5-20250929")),
            ("llm_budget_usd", None),
            ("llm_budget_window_days", Some("30")),
            ("inference_base_url", None),
            ("embedding_provider", Some("local_bge")),
            ("embedding_model", Some("BAAI/bge-m3")),
            ("embedding_dim", Some("1024")),
            ("embedding_base_url", None),
            ("embedding_timeout", Some("120.0")),
            ("embedding_api_key", None),
            ("reranker_provider", Some("local_bge")),
            ("reranker_model", Some("BAAI/bge-reranker-v2-m3")),
            ("reranker_timeout", Some("30.0")),
            ("data_dir", None),
            ("plugins_dir", None),
            ("works_dir", None),
            ("works_policy", None),
            ("ingest_concurrency", Some("4")),
            ("embedding_batch_size", Some("32")),
            ("docling_device", Some("cpu")),
            ("docling_max_workers", None),
            ("docling_pages_per_task", Some("50")),
            ("extraction_concurrency", Some("8")),
            ("extraction_retry_on_validation", Some("True")),
            ("default_language", None),
            ("search_window_max_tokens", Some("1500")),
            ("search_window_min_tokens", Some("200")),
            ("hnsw_ef_search", Some("100")),
            ("log_level", Some("INFO")),
            ("log_format", Some("pretty")),
        ];
        assert_eq!(SETTING_FIELDS.len(), table.len());
        for (i, (field, default)) in table.iter().enumerate() {
            assert_eq!(SETTING_FIELDS[i], *field, "declaration order");
            assert_eq!(default_str(field), *default, "{field}");
        }
        assert_eq!(default_str("no_such_setting"), None);
    }

    #[test]
    fn env_var_names_carry_the_prefix() {
        assert_eq!(env_var_name("db_url"), "RE_DB_URL");
        assert_eq!(env_var_name("default_language"), "RE_DEFAULT_LANGUAGE");
    }

    #[test]
    fn literal_options_validate() {
        assert!(valid_option("llm_provider", "anthropic"));
        assert!(valid_option("llm_provider", "openai_compatible"));
        assert!(!valid_option("llm_provider", "other"));
        assert!(valid_option("embedding_provider", "auto"));
        assert!(!valid_option("embedding_provider", "none"));
        assert!(valid_option("reranker_provider", "none"));
        assert!(!valid_option("reranker_provider", "sometimes"));
        assert!(valid_option("docling_device", "cuda"));
        assert!(!valid_option("docling_device", "tpu"));
        assert!(valid_option("log_format", "json"));
        assert!(!valid_option("log_format", "yaml"));
        assert!(valid_option("db_url", "anything"));
    }

    #[test]
    fn default_data_dir_lives_under_home() {
        // `Settings().data_dir` is `~/.research-engine`; `None` only when
        // no home is discoverable at all.
        assert!(default_data_dir().is_none_or(|dir| dir.ends_with(".research-engine")));
    }

    #[test]
    fn resolved_plugins_dir_defaults_under_data_dir() {
        assert_eq!(
            resolve_plugins_dir(None, Some("/data")).as_deref(),
            Some("/data/plugins")
        );
        assert_eq!(
            resolve_plugins_dir(Some("/custom"), Some("/data")).as_deref(),
            Some("/custom")
        );
        assert_eq!(resolve_plugins_dir(None, None), None);
    }

    #[test]
    fn inference_url_honours_the_deprecated_name() {
        assert_eq!(
            resolve_inference_base_url(Some("http://gpu:9882"), Some("http://old:9882")).as_deref(),
            Some("http://gpu:9882")
        );
        assert_eq!(
            resolve_inference_base_url(None, Some("http://old:9882")).as_deref(),
            Some("http://old:9882")
        );
        assert_eq!(resolve_inference_base_url(Some(""), Some("")), None);
        assert_eq!(resolve_inference_base_url(None, None), None);
    }

    #[test]
    fn missing_works_dir_is_a_loud_refusal() {
        let err = require_works_dir(None).expect_err("unset works dir refuses");
        assert_eq!(
            err.to_string(),
            "RE_WORKS_DIR is not set, so no work file can be read. \
             Point it at the folder holding works/*.md."
        );
        assert_eq!(require_works_dir(Some("/works")).as_deref(), Ok("/works"));
    }

    #[test]
    fn env_file_keys_tolerate_comments_blanks_and_export() {
        let keys = parse_env_file_keys(
            "# a comment\n\nexport RE_DEFAULT_LANGUAGE=en\n  RE_SEARCH_WINDOW_MAX_TOKENS = 40 \nMALFORMED_LINE\n",
        );
        assert!(keys.contains("RE_DEFAULT_LANGUAGE"));
        assert!(keys.contains("RE_SEARCH_WINDOW_MAX_TOKENS"));
        assert_eq!(keys.len(), 2);
        assert!(parse_env_file_keys("").is_empty());
    }

    fn write(path: &Path, body: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, body).unwrap();
    }

    #[test]
    fn finds_env_file_in_the_starting_directory() {
        let root = std::env::temp_dir().join(format!("re_phase7_{}_a", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        write(&root.join("pyproject.toml"), "[project]\nname='x'\n");
        write(&root.join(".env"), "RE_DEFAULT_LANGUAGE=en\n");
        let resolution = find_env_file(&root, None);
        assert!(resolution.exists());
        assert_eq!(resolution.path, Some(root.join(".env")));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn finds_env_file_from_a_subdirectory() {
        let root = std::env::temp_dir().join(format!("re_phase7_{}_b", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        write(&root.join("pyproject.toml"), "[project]\nname='x'\n");
        write(&root.join(".env"), "RE_DEFAULT_LANGUAGE=en\n");
        let sub = root.join("a").join("b");
        std::fs::create_dir_all(&sub).unwrap();
        let resolution = find_env_file(&sub, None);
        assert_eq!(resolution.path, Some(root.join(".env")));
        assert!(resolution.reason.contains(&root.display().to_string()));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn search_stops_at_the_project_root() {
        let root = std::env::temp_dir().join(format!("re_phase7_{}_c", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        write(&root.join("pyproject.toml"), "[project]\nname='x'\n");
        let sub = root.join("sub");
        std::fs::create_dir_all(&sub).unwrap();
        let resolution = find_env_file(&sub, None);
        assert!(!resolution.exists());
        assert!(resolution.reason.contains("no .env found"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn search_without_any_project_root_reaches_the_filesystem_root() {
        // No pyproject anywhere: the walk ends at `parent() == None`.
        let root = std::env::temp_dir().join(format!("re_phase7_{}_e", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let sub = root.join("sub");
        std::fs::create_dir_all(&sub).unwrap();
        let resolution = find_env_file(&sub, None);
        assert_eq!(resolution.path, None);
        assert!(resolution.reason.contains("no .env found"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn empty_override_behaves_like_no_override() {
        let root = std::env::temp_dir().join(format!("re_phase7_{}_f", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        write(&root.join("pyproject.toml"), "[project]\nname='x'\n");
        write(&root.join(".env"), "RE_DEFAULT_LANGUAGE=en\n");
        let resolution = find_env_file(&root, Some(""));
        assert_eq!(resolution.path, Some(root.join(".env")));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn tilde_expands_against_home() {
        use std::ffi::OsStr;
        let home = OsStr::new("/home/someone");
        assert_eq!(
            expand_user("~/custom.env", Some(home)),
            Path::new("/home/someone/custom.env")
        );
        assert_eq!(expand_user("~", Some(home)), Path::new("/home/someone"));
        assert_eq!(expand_user("~/custom.env", None), Path::new("~/custom.env"));
        assert_eq!(expand_user("~", None), Path::new("~"));
        assert_eq!(
            expand_user("relative.env", Some(home)),
            Path::new("relative.env")
        );
        assert_eq!(
            expand_user("~other/custom.env", Some(home)),
            Path::new("~other/custom.env")
        );
    }

    #[test]
    fn stale_paths_report_not_existing() {
        let resolution = EnvFileResolution {
            path: Some(Path::new("/no/such/file.env").to_owned()),
            reason: "nearest .env above /no/such".to_owned(),
        };
        assert!(!resolution.exists());
    }
    #[test]
    fn explicit_override_wins_and_missing_is_loud() {
        let root = std::env::temp_dir().join(format!("re_phase7_{}_d", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        write(&root.join("pyproject.toml"), "[project]\nname='x'\n");
        write(&root.join(".env"), "RE_DEFAULT_LANGUAGE=en\n");
        let elsewhere = root.join("custom.env");
        write(&elsewhere, "RE_DEFAULT_LANGUAGE=de\n");
        let resolution = find_env_file(&root, Some(elsewhere.to_str().unwrap()));
        assert_eq!(resolution.path, Some(elsewhere.canonicalize().unwrap()));
        assert_eq!(resolution.reason, "RE_ENV_FILE");
        let missing = find_env_file(
            &root,
            Some(root.join("absent.env").to_str().unwrap().to_owned()).as_deref(),
        );
        assert!(!missing.exists());
        assert!(missing.reason.contains("(missing)"));
        let _ = std::fs::remove_dir_all(&root);
    }

    fn maps(env: &[(&str, &str)], file: &[&str]) -> (HashMap<String, String>, HashSet<String>) {
        (
            env.iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect(),
            file.iter().map(|k| (*k).to_owned()).collect(),
        )
    }

    #[test]
    fn values_resolve_by_precedence() {
        let (environ, _) = maps(&[("RE_DEFAULT_LANGUAGE", "fr")], &[]);
        let mut file_values = HashMap::new();
        file_values.insert("RE_DEFAULT_LANGUAGE".to_owned(), "de".to_owned());
        // Environment beats the file.
        assert_eq!(
            resolve_precedence("default_language", None, &environ, &file_values, "unset"),
            (SettingSource::Environment, "fr".to_owned())
        );
        // The file beats the default.
        assert_eq!(
            resolve_precedence(
                "default_language",
                None,
                &HashMap::new(),
                &file_values,
                "unset"
            ),
            (SettingSource::EnvFile, "de".to_owned())
        );
        // Overrides beat everything.
        assert_eq!(
            resolve_precedence(
                "default_language",
                Some("it"),
                &environ,
                &file_values,
                "unset"
            ),
            (SettingSource::Override, "it".to_owned())
        );
        // Nothing set means the default.
        assert_eq!(
            resolve_precedence(
                "default_language",
                None,
                &HashMap::new(),
                &HashMap::new(),
                "unset"
            ),
            (SettingSource::Default, "unset".to_owned())
        );
    }

    #[test]
    fn report_attributes_each_value_to_its_source() {
        let (environ, _) = maps(&[("RE_SEARCH_WINDOW_MAX_TOKENS", "50")], &[]);
        let (_, file_keys) = maps(&[], &["RE_DEFAULT_LANGUAGE"]);
        let overrides: HashSet<String> = ["log_level".to_owned()].into_iter().collect();
        assert_eq!(
            attribute_source("default_language", &overrides, &environ, &file_keys),
            SettingSource::EnvFile
        );
        assert_eq!(
            attribute_source("search_window_max_tokens", &overrides, &environ, &file_keys),
            SettingSource::Environment
        );
        assert_eq!(
            attribute_source("search_window_min_tokens", &overrides, &environ, &file_keys),
            SettingSource::Default
        );
        assert_eq!(
            attribute_source("log_level", &overrides, &environ, &file_keys),
            SettingSource::Override
        );
        // A value equal to the default still attributes to the file.
        let (_, file_keys) = maps(&[], &["RE_DB_URL"]);
        assert_eq!(
            attribute_source("db_url", &HashSet::new(), &HashMap::new(), &file_keys),
            SettingSource::EnvFile
        );
        assert_eq!(SettingSource::EnvFile.as_str(), "env file");
        assert_eq!(SettingSource::Override.as_str(), "override");
        assert_eq!(SettingSource::Environment.as_str(), "environment");
        assert_eq!(SettingSource::Default.as_str(), "default");
    }

    #[test]
    fn report_never_prints_secret_values() {
        assert_eq!(
            mask_value("anthropic_api_key", Some("sk-ant-supersecret")),
            "SET"
        );
        assert_eq!(mask_value("anthropic_api_key", Some("")), "SET");
        assert_eq!(mask_value("anthropic_api_key", None), "unset");
        assert_eq!(mask_value("db_url", Some("postgres://x")), "postgres://x");
        assert_eq!(mask_value("default_language", None), "unset");
    }
}
