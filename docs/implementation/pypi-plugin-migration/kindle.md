# Kindle plugin implementation guide

**Repository:** `John-Cusack/marginalia-plugin-kindle`  
**Target release:** `marginalia-ai-plugin-kindle 0.4.0`  
**Prerequisite:** production `marginalia-ai-sdk==0.6.0` and `marginalia-ai==0.6.0`  
**Current source state:** clean `main`, current version/tag `0.3.1`

## 0. Baseline

```bash
uv sync --extra dev
uv run ruff check kindle tests
uv run pytest tests/unit -q
uv build --out-dir /tmp/kindle-before
```

Record a real `kindle.list_books`/`kindle.check_book` response against existing local state;
do not scrape or ingest merely to establish baseline.

## 1. Convert package metadata

Update `pyproject.toml`:

- name/version: `marginalia-ai-plugin-kindle`, `0.4.0`;
- dependency: `marginalia-ai-sdk>=0.6,<0.7`;
- retain Playwright, pytesseract, and Pillow as runtime dependencies;
- remove EasyOCR from base dependencies;
- add `gpu-ocr = ["easyocr>=1.7"]`;
- add complete author, README, Apache license file, classifiers, keywords, and URLs;
- add `research-engine-kindle-setup` console script for explicit browser setup/diagnostics;
- add entry point:

```toml
[project.entry-points."research_engine.plugins"]
kindle = "kindle"
```

Use a Hatchling minimum that supports the selected license metadata. Ensure `LICENSE` and
README enter wheel/sdist metadata.

## 2. Move and convert the manifest

Move root `pack.yaml` to `kindle/plugin.yaml`; remove the root copy.

Convert to schema v2:

- `plugin_id: kindle`;
- core range `>=0.6,<0.7`;
- keep network, subprocess, filesystem, and ingest permissions;
- preserve `kindle_book` and all four MCP tools;
- preserve complete input schemas from decorators or put them in the manifest;
- remove identity/version/license, `requires.pip`, and `setup_commands`.

The wheel must include `kindle/plugin.yaml`. Entry modules must remain below `kindle`.

## 3. Migrate SDK imports

Replace every:

```text
research_engine.plugins.sdk
```

with `research_engine_sdk`.

Remove `pytest.importorskip("research_engine")` from unit tests. Unit tests depend on SDK and
plugin code only. Add one contract test that imports every declared handler with core absent.

No runtime file may import `research_engine.*` when this phase is complete.

Gate:

```bash
python -m pip install marginalia-ai-sdk==0.6.0
grep-equivalent repository check: no runtime match for "research_engine."
uv run pytest tests/unit -q
```

Use the repository's Python search/check tooling rather than shell text assertions in permanent
tests.

## 4. Use SDK ingestion instead of core chunkers

In `kindle/tools/ingest_book.py`:

- remove `ProseWindowChunker` import and manual `chunk()` call;
- keep idempotency, scraping/cache behavior, metadata, source, title, and error responses;
- call SDK `ingestion.ingest_document()` with full text and `document_type="kindle_book"`;
- let core resolve the `prose_window` default declared by the plugin manifest;
- ensure canonical full text reaches core so offsets remain verifiable.

Update tests so they assert the consumer-visible ingestion request/result, not internal field
forwarding. Include one integration test against core `0.6.0` proving text is chunked, embedded
with the fixture embedder, stored once, and returned as the same existing document on repeat.

## 5. Put mutable data behind `PluginContext`

Replace hard-coded `~/.marginalia/plugins/kindle` in `kindle/_paths.py`.

- Path functions accept or resolve the injected SDK `PluginContext.data_dir`.
- Tools receive `context` with scoped clients.
- Console/browser setup obtains the same path through SDK/config helper or a documented
  `RE_PLUGIN_DATA_DIR` override.
- Default new location is `~/.research-engine/plugin-data/kindle`.

Provide an explicit data migration command in `research-engine-kindle-setup`:

- detect old cookies/extracted texts;
- show old/new paths and files before moving;
- refuse overwrite when destination contains different data;
- copy/move only after explicit confirmation;
- preserve file permissions;
- never delete the old directory until verification succeeds.

Tests cover absent old data, successful migration, identical destination, and conflicting
destination.

## 6. Make OCR genuinely optional

`kindle/scraper/ocr.py` already lazy-imports EasyOCR. Complete the boundary:

- if EasyOCR is absent and a Kryptonite/canvas page requires it, return one actionable error
  naming `marginalia-ai-plugin-kindle[gpu-ocr]`;
- allow CPU EasyOCR only when explicitly configured; default GPU behavior must report missing
  CUDA clearly rather than crash;
- keep DOM extraction as the normal path;
- document the system Tesseract executable required by pytesseract;
- test normal DOM scraping in an environment without Torch/EasyOCR;
- test missing-extra behavior without installing EasyOCR;
- run a separate optional CI job for `gpu-ocr` import/smoke.

The base Kindle wheel must not require Torch or any NVIDIA package.

## 7. Replace automatic Playwright setup

Delete manifest `playwright install chromium` behavior; core no longer supports setup commands.

`research-engine-kindle-setup` must:

1. report Playwright/Chromium availability;
2. print the exact browser-install action;
3. run browser installation only after explicit user invocation;
4. verify a visible browser can start;
5. report Tesseract and optional EasyOCR status;
6. never request Amazon credentials on stdin or store passwords.

Keep first-run interactive Amazon login and cookie reuse. Document that browser windows are
required and that enabled plugin code is trusted in-process code.

## 8. Discovery and permission integration

Against core `0.6.0`, test:

1. pip install exposes entry point but does not import or enable Kindle;
2. `plugin list` reports `available`;
3. `plugin audit kindle` shows all permissions/contributions without import;
4. `plugin enable kindle --yes` records version/hash/permissions;
5. restart/load registers exactly four namespaced tools and `kindle_book`;
6. a changed manifest requires `approve-upgrade`;
7. disabling removes tools from the next runtime catalogue without deleting data.

Add a sentinel in `kindle/__init__.py` fixture/test setup to prove discovery itself performs no
import side effect.

## 9. Documentation and changelog

Update README with:

- pip and pipx install commands;
- explicit setup command;
- base versus `[gpu-ocr]` installs;
- enable/audit/disable commands;
- data location/migration;
- Amazon login and licensed-content warning;
- system Tesseract requirement;
- core/SDK compatibility;
- troubleshooting and issue/security links.

Add `0.4.0` changelog section describing distribution rename, SDK cutover, data migration,
entry-point activation, removal of automatic setup, and optional GPU OCR.

## 10. Release CI and final gate

Add normal CI for Python 3.11–3.13, unit tests, lint, SDK-only import, artifact content, and a
core integration job. Add protected Trusted Publishing workflow.

Final commands:

```bash
uv lock
uv run ruff check kindle tests
uv run pytest tests/unit -q
uv build
uvx --from twine twine check --strict dist/*
```

Clean artifact smoke:

```bash
python -m venv /tmp/kindle-release-smoke
/tmp/kindle-release-smoke/bin/python -m pip install --upgrade pip
/tmp/kindle-release-smoke/bin/python -m pip install marginalia-ai==0.6.0 dist/*.whl
/tmp/kindle-release-smoke/bin/python -c 'from importlib.util import find_spec; assert find_spec("torch") is None'
/tmp/kindle-release-smoke/bin/research-engine plugin list
```

With disposable `RE_DB_URL`, enable Kindle and run `kindle.list_books` plus a fixture-backed
`kindle.ingest_book` integration path. Do not publish until no repository checkout is on
`PYTHONPATH` and every artifact/resource/permission assertion passes.

Tag `v0.4.0`, publish through Trusted Publishing, then repeat install/discovery/enable smoke
from production PyPI.
