"""Postgres repository implementations."""

from research_engine.adapters.storage.postgres.repositories.document_texts import (
    PGDocumentTextRepo,
)
from research_engine.adapters.storage.postgres.repositories.documents import PGDocumentRepo
from research_engine.adapters.storage.postgres.repositories.edges import PGEdgeRepo
from research_engine.adapters.storage.postgres.repositories.entities import PGEntityRepo
from research_engine.adapters.storage.postgres.repositories.events import PGEventRepo
from research_engine.adapters.storage.postgres.repositories.extractions import (
    PGExtractionRepo,
    PGExtractionSchemaRepo,
)
from research_engine.adapters.storage.postgres.repositories.mentions import PGMentionRepo
from research_engine.adapters.storage.postgres.repositories.passages import PGPassageRepo
from research_engine.adapters.storage.postgres.repositories.plugins import PGInstalledPluginRepo
from research_engine.adapters.storage.postgres.repositories.provenance import (
    PGIngestionRunRepo,
    PGLLMCallLogRepo,
)

__all__ = [
    "PGDocumentRepo",
    "PGDocumentTextRepo",
    "PGEdgeRepo",
    "PGEntityRepo",
    "PGEventRepo",
    "PGExtractionRepo",
    "PGExtractionSchemaRepo",
    "PGIngestionRunRepo",
    "PGInstalledPluginRepo",
    "PGLLMCallLogRepo",
    "PGMentionRepo",
    "PGPassageRepo",
]
