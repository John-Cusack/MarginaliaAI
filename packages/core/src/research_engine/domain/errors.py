"""Error hierarchy for the research engine."""

from __future__ import annotations


class ResearchEngineError(Exception):
    """Base for all engine errors."""


class ConfigurationError(ResearchEngineError):
    """Invalid or missing configuration."""


# --- Ingestion ---


class IngestionError(ResearchEngineError):
    """Error during document ingestion."""


class DispatchMiss(IngestionError):
    """No ingestion module matched the source."""

    def __init__(self, source_ref: str) -> None:
        self.source_ref = source_ref
        super().__init__(f"No ingestion module matched source: {source_ref}")


class DispatchError(IngestionError):
    """Hinted module rejected the source."""


class ParseError(IngestionError):
    """Parsing a document failed."""


class ChunkingError(IngestionError):
    """Chunking a document failed."""


# --- Validation ---


class ValidationError(ResearchEngineError):
    """Data validation failed."""


class EvidenceNotFound(ValidationError):
    """Evidence span not found in passage text."""

    def __init__(self, field: str, passage_id: object, span_text: str) -> None:
        self.field = field
        self.passage_id = passage_id
        self.span_text = span_text
        super().__init__(
            f"Evidence span for field '{field}' not found in passage {passage_id}: "
            f"'{span_text[:100]}'"
        )


# --- Search ---


class UnsupportedFilterError(ValidationError, ValueError):
    """A filter key reached the repository that it does not implement.

    Raised rather than ignored so that ``SearchResult.applied_filters`` is true
    by construction: a filter that survives to the query was applied.  Also a
    ``ValueError`` so that MCP tools accepting raw filter dicts report it as
    invalid input rather than an internal failure.
    """

    def __init__(self, unknown: list[str], supported: list[str]) -> None:
        self.unknown = unknown
        self.supported = supported
        super().__init__(
            f"Unsupported filter key(s): {unknown}. Supported keys: {supported}"
        )


class UnknownFilterExtension(ValidationError, ValueError):
    """A filter extension was requested but is not registered."""

    def __init__(self, extension_id: str, available: list[str]) -> None:
        self.extension_id = extension_id
        self.available = available
        hint = f"Available extensions: {available}" if available else (
            "No filter extensions are registered. If this filter comes from a "
            "pack, check that the pack is enabled."
        )
        super().__init__(f"Unknown filter extension: '{extension_id}'. {hint}")


# --- LLM ---


class LLMError(ResearchEngineError):
    """Error from an LLM provider."""


class LLMProviderDown(LLMError):
    """LLM provider is unreachable."""


class LLMRateLimited(LLMError):
    """LLM provider rate limit exceeded."""


class LLMUnavailable(LLMError):
    """The LLM is not configured, or will not authenticate. No call can succeed.

    The counterpart of `EmbeddingUnavailable`, and it exists for the same
    reason: a per-item failure and a dead backend need different handling. An
    extraction run marks a failed passage and moves on, which is right when one
    passage tripped a content filter and catastrophic when the API key is
    missing — the run writes a failed row for every passage in the corpus,
    reports thousands of errors, and none of them says "there is no key".

    Missing credentials surface from the Anthropic client as a bare `TypeError`
    raised before any request, so nothing downstream recognised them as an LLM
    problem at all.
    """


# --- Plugin ---


class PluginError(ResearchEngineError):
    """Error related to the plugin system."""


class PluginLoadError(PluginError):
    """Failed to load a plugin."""


class PluginConflict(PluginError):
    """Two plugins register the same contribution."""

    def __init__(self, contribution_id: str, existing_plugin: str, new_plugin: str) -> None:
        self.contribution_id = contribution_id
        self.existing_plugin = existing_plugin
        self.new_plugin = new_plugin
        super().__init__(
            f"Conflict: '{contribution_id}' already registered by '{existing_plugin}', "
            f"cannot register from '{new_plugin}'"
        )


class PermissionDenied(PluginError):
    """Plugin tried to use a capability it wasn't granted."""

    def __init__(self, plugin: str, permission: str) -> None:
        self.plugin = plugin
        self.permission = permission
        super().__init__(
            f"Plugin '{plugin}' lacks permission '{permission}'. "
            f"Add it to the permissions section of pack.yaml."
        )


class PluginConfigError(PluginError):
    """Plugin configuration is missing or invalid."""


# --- Types ---


class UnknownType(ResearchEngineError):
    """Referenced type is not registered."""

    def __init__(self, kind: str, type_id: str, hint: str = "") -> None:
        self.kind = kind
        self.type_id = type_id
        msg = f"Unknown {kind}: '{type_id}'"
        if hint:
            msg += f". {hint}"
        super().__init__(msg)


def describe_exception(exc: BaseException) -> str:
    """A description that is never empty, for logs and wrapped messages.

    ``str(exc)`` is the natural thing to log and it is not sufficient: several
    exceptions that matter here carry no message at all. ``httpx.ConnectTimeout``
    is the one that cost real time — ``str()`` on it returns ``''``, so an
    embedding run logged ``error=`` thousands of times while a GPU host sat
    powered off, and the logs looked like progress rather than failure.

    The type name is always present because the type is often the whole story.
    """
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


# --- Embedding ---


class EmbeddingUnavailable(ResearchEngineError):
    """The embedding backend cannot be reached, and a smaller batch will not help.

    Distinct from an ordinary batch failure, which is usually accelerator memory
    pressure and *is* fixed by halving. This one says the backend is not there:
    the same call fails identically at any size, so every retry costs a full
    timeout and buys nothing.

    The distinction is not academic. A corpus re-chunk once ran for hours
    halving 16 -> 8 -> 4 -> 2 -> 1 against a GPU host that had been powered off
    for a day, wrote nothing, and reported it as `error=` — an empty string,
    because `str(httpx.ConnectTimeout())` is empty. Raising this instead stops
    the run at the first batch and names the cause.
    """


# --- Storage ---


class StorageError(ResearchEngineError):
    """Database or storage error."""


class NotFoundError(ResearchEngineError):
    """Requested entity not found."""

    def __init__(self, kind: str, id: object) -> None:
        self.kind = kind
        self.id = id
        super().__init__(f"{kind} not found: {id}")
