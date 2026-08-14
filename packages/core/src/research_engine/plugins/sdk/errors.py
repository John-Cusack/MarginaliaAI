"""SDK error re-exports for plugin authors."""

from research_engine.domain.errors import (
    PermissionDenied,
    PluginConfigError,
    UnknownType,
    ValidationError,
)

__all__ = ["PermissionDenied", "UnknownType", "ValidationError", "PluginConfigError"]
