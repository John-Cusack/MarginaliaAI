"""Public exceptions that plugins may raise or handle."""

from __future__ import annotations


class ResearchEngineError(Exception):
    """Base for transport-safe SDK errors."""


class ConfigurationError(ResearchEngineError):
    """Runtime configuration is missing or invalid."""


class ValidationError(ResearchEngineError):
    """Plugin-supplied data failed validation."""


class PluginConfigError(ConfigurationError):
    """Plugin configuration is missing or invalid."""


class PermissionDenied(ResearchEngineError):
    """A plugin requested a capability it was not approved to use."""

    def __init__(self, plugin: str, permission: str) -> None:
        self.plugin = plugin
        self.permission = permission
        super().__init__(
            f"Plugin '{plugin}' lacks permission '{permission}'. "
            "Approve it in the permissions section of plugin.yaml before use."
        )


class UnknownType(ValidationError):
    def __init__(self, kind: str, type_id: str, hint: str = "") -> None:
        self.kind = kind
        self.type_id = type_id
        message = f"Unknown {kind}: '{type_id}'"
        if hint:
            message += f". {hint}"
        super().__init__(message)


class IngestionError(ResearchEngineError):
    """Document ingestion failed."""


class EmbeddingUnavailable(IngestionError):
    """The configured embedding backend cannot currently serve ingestion."""
