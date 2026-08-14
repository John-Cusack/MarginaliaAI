"""ISO 639-1 language codes to Postgres text-search configurations.

Postgres stems text according to a ``regconfig``. Indexing German under the
English stemmer does not fail — it silently produces the wrong lexemes, so
keyword recall degrades while vector recall (bge-m3 is multilingual) stays fine,
and RRF then fuses a good ranked list with a bad one.

The fallback is ``simple``, never ``english``. ``simple`` does no stemming at
all, which degrades *gracefully* for an unknown language; ``english`` degrades
*wrongly*, and does so invisibly.
"""

from __future__ import annotations

#: Snowball configurations shipped with Postgres since 10.
_ISO_TO_PG: dict[str, str] = {
    "ar": "arabic",
    "da": "danish",
    "de": "german",
    "el": "greek",
    "en": "english",
    "es": "spanish",
    "eu": "basque",
    "fi": "finnish",
    "fr": "french",
    "ga": "irish",
    "hi": "hindi",
    "hu": "hungarian",
    "hy": "armenian",
    "id": "indonesian",
    "it": "italian",
    "lt": "lithuanian",
    "ne": "nepali",
    "nl": "dutch",
    "no": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sr": "serbian",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
    "yi": "yiddish",
}

#: The safe fallback: no stemming, no wrong stemming.
DEFAULT_CONFIG = "simple"

#: Every regconfig this module can produce. Used to reject anything else before
#: it reaches SQL — a config name is interpolated into the query text, not bound
#: as a parameter, because Postgres requires a literal regconfig there.
KNOWN_CONFIGS = frozenset(_ISO_TO_PG.values()) | {DEFAULT_CONFIG}


def pg_config(iso: str | None) -> str:
    """Map an ISO 639-1 code (or full locale like ``de-CH``) to a regconfig.

    Anything unrecognised — including ``None`` — maps to ``simple``.
    """
    if not iso:
        return DEFAULT_CONFIG
    return _ISO_TO_PG.get(iso.strip().lower()[:2], DEFAULT_CONFIG)


def is_known_config(config: str) -> bool:
    """Whether *config* is a regconfig this module vouches for.

    Guards SQL construction: ``lang_config`` values read back from the database
    are interpolated as literals, so they must be validated first.
    """
    return config in KNOWN_CONFIGS
