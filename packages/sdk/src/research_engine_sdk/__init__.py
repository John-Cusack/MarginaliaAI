"""Research Engine Plugin SDK — thin re-export of core SDK surface.

Plugin authors should depend only on `research-engine-sdk`, not on
`research-engine` core. This ensures a minimal, stable API surface.
"""

# This package re-exports from the core SDK when installed alongside core.
# When used standalone, it provides the type stubs only.
try:
    from research_engine.plugins.sdk import (
        Chunker,
        # Clients
        CorpusClient,
        # Types
        DetectionResult,
        # Domain types
        Document,
        Edge,
        Entity,
        EntityClient,
        Event,
        EventClient,
        ExtractionClient,
        ExtractionRecord,
        FuzzyDate,
        HttpClient,
        # Interfaces
        IngestionModule,
        LLMClient,
        Mention,
        ParsedDocument,
        Passage,
        PassageDraft,
        # Errors
        PermissionDenied,
        PostIngestionHook,
        SourceRef,
        UnknownType,
        ValidationError,
        hook,
        # Decorators
        tool,
    )

    __all__ = [
        "DetectionResult", "FuzzyDate", "ParsedDocument", "SourceRef",
        "IngestionModule", "Chunker", "PostIngestionHook",
        "CorpusClient", "EntityClient", "EventClient", "ExtractionClient",
        "HttpClient", "LLMClient",
        "tool", "hook",
        "PermissionDenied", "UnknownType", "ValidationError",
        "Document", "Passage", "PassageDraft", "Entity", "Mention",
        "Event", "Edge", "ExtractionRecord",
    ]
except ImportError:
    # Standalone SDK — types only, for plugin development without core installed
    pass
