"""Plugin SDK — public surface for plugin authors."""

# Re-export domain types for convenience
from research_engine.domain.documents import Document
from research_engine.domain.edges import Edge
from research_engine.domain.entities import Entity, Mention
from research_engine.domain.events import Event
from research_engine.domain.extractions import ExtractionRecord
from research_engine.domain.passages import Passage, PassageDraft
from research_engine.domain.source_search import (
    Availability,
    IngestAction,
    SourceMatch,
    SourceQuery,
    SourceSearchProvider,
)
from research_engine.plugins.sdk.clients import (
    CorpusClient,
    EdgeClient,
    EntityClient,
    EventClient,
    ExtractionClient,
    HttpClient,
    IngestionClient,
    LLMClient,
)
from research_engine.plugins.sdk.decorators import hook, tool
from research_engine.plugins.sdk.errors import PermissionDenied, UnknownType, ValidationError
from research_engine.plugins.sdk.interfaces import Chunker, IngestionModule, PostIngestionHook
from research_engine.plugins.sdk.types import (
    DetectionResult,
    FuzzyDate,
    ParsedDocument,
    SourceRef,
)

__all__ = [
    # SDK types
    "DetectionResult", "FuzzyDate", "ParsedDocument", "SourceRef",
    # Interfaces
    "IngestionModule", "Chunker", "PostIngestionHook", "SourceSearchProvider",
    # Source search domain
    "SourceQuery", "SourceMatch", "IngestAction", "Availability",
    # Clients
    "CorpusClient", "EdgeClient", "EntityClient", "EventClient", "ExtractionClient",
    "HttpClient", "IngestionClient", "LLMClient",
    # Decorators
    "tool", "hook",
    # Errors
    "PermissionDenied", "UnknownType", "ValidationError",
    # Domain types
    "Document", "Passage", "PassageDraft", "Entity", "Mention",
    "Event", "Edge", "ExtractionRecord",
]
