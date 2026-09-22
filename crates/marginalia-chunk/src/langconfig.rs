//! ISO 639-1 language codes to Postgres text-search configurations,
//! mirroring `services/search/langconfig.py`.
//!
//! Postgres stems text according to a `regconfig`. Indexing German under the
//! English stemmer does not fail — it silently produces the wrong lexemes.
//! The fallback is `simple`, never `english`: `simple` does no stemming,
//! which degrades gracefully for an unknown language.

/// The safe fallback: no stemming, no wrong stemming.
pub const DEFAULT_CONFIG: &str = "simple";

/// Every regconfig this module can produce. Used to reject anything else
/// before it reaches SQL — a config name is interpolated into the query
/// text, not bound as a parameter, because Postgres requires a literal
/// regconfig there.
pub const KNOWN_CONFIGS: &[&str] = &[
    "arabic",
    "basque",
    "danish",
    "dutch",
    "english",
    "finnish",
    "french",
    "german",
    "greek",
    "hindi",
    "hungarian",
    "armenian",
    "indonesian",
    "irish",
    "italian",
    "lithuanian",
    "nepali",
    "norwegian",
    "portuguese",
    "romanian",
    "russian",
    "serbian",
    "simple",
    "spanish",
    "swedish",
    "tamil",
    "turkish",
    "yiddish",
];

/// Map an ISO 639-1 code (or a full locale like `de-CH`) to a regconfig.
///
/// Anything unrecognised — including `None` — maps to `simple`. Only the
/// first two characters past stripping and lowercasing are consulted,
/// exactly as upstream.
pub fn pg_config(iso: Option<&str>) -> &'static str {
    let Some(iso) = iso else {
        return DEFAULT_CONFIG;
    };
    let lowered = iso.trim().to_lowercase();
    let prefix: String = lowered.chars().take(2).collect();
    match prefix.as_str() {
        "ar" => "arabic",
        "da" => "danish",
        "de" => "german",
        "el" => "greek",
        "en" => "english",
        "es" => "spanish",
        "eu" => "basque",
        "fi" => "finnish",
        "fr" => "french",
        "ga" => "irish",
        "hi" => "hindi",
        "hu" => "hungarian",
        "hy" => "armenian",
        "id" => "indonesian",
        "it" => "italian",
        "lt" => "lithuanian",
        "ne" => "nepali",
        "nl" => "dutch",
        "no" => "norwegian",
        "pt" => "portuguese",
        "ro" => "romanian",
        "ru" => "russian",
        "sr" => "serbian",
        "sv" => "swedish",
        "ta" => "tamil",
        "tr" => "turkish",
        "yi" => "yiddish",
        _ => DEFAULT_CONFIG,
    }
}

/// Whether `config` is a regconfig this module vouches for.
///
/// Guards SQL construction: `lang_config` values read back from the database
/// are interpolated as literals, so they must be validated first.
pub fn is_known_config(config: &str) -> bool {
    KNOWN_CONFIGS.contains(&config)
}
