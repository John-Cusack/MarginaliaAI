"""Corpus Engine — turn a personal library into a research superpower."""

import sys

import structlog

__version__ = "0.6.1"

# structlog's unconfigured default prints to stdout, and under `serve` stdout *is*
# the MCP stdio transport. Anything logged before `runtime.configure_logging` runs
# — `settings_loaded`, for one — would otherwise land in the middle of the
# JSON-RPC stream. `configure_once` leaves an application that configured
# structlog before importing us alone.
structlog.configure_once(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
