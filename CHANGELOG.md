# Changelog

## 0.2.0 — 2026-05-05

### Added
- `IngestionClient.find_existing(source=..., source_pattern=...)` — plugins can now look
  up already-ingested documents by exact source path or substring match without reaching
  into the corpus schema. Backed by `IngestionOrchestrator.find_existing` and stubbed in
  `DeniedIngestionClient` so denied callers fail loudly with `PermissionDenied`.

### Notes
- `IngestionClient` is a `Protocol`; adding a method is technically a breaking change for
  any out-of-tree implementations. Bundled implementations are updated.
- Plugins relying on the new method should declare `requires.core_api: ">=0.2.0,<1.0.0"`
  in `pack.yaml`. (No runtime enforcement yet — this is documentation.)
