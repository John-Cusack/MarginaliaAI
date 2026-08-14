"""Abstract port interfaces — the boundary between core and adapters."""

from research_engine.ports.clock import ClockPort
from research_engine.ports.embedding import EmbeddingPort
from research_engine.ports.http import HttpPort
from research_engine.ports.llm import LLMPort
from research_engine.ports.repositories import (
    DocumentRepo,
    EdgeRepo,
    EntityRepo,
    EventRepo,
    ExtractionRepo,
    ExtractionSchemaRepo,
    IngestionRunRepo,
    InstalledPluginRepo,
    LLMCallLogRepo,
    MentionRepo,
    PassageRepo,
)
from research_engine.ports.reranker import RerankerPort

__all__ = [
    "ClockPort",
    "DocumentRepo",
    "EdgeRepo",
    "EmbeddingPort",
    "EntityRepo",
    "EventRepo",
    "ExtractionRepo",
    "ExtractionSchemaRepo",
    "HttpPort",
    "IngestionRunRepo",
    "InstalledPluginRepo",
    "LLMCallLogRepo",
    "LLMPort",
    "MentionRepo",
    "PassageRepo",
    "RerankerPort",
]
