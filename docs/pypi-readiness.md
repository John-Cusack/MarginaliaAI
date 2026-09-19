# PyPI readiness review and release plan

**Architecture proposal:** [PyPI distribution and plugin architecture](design/pypi-plugin-distribution-architecture.md)

## Verdict

**Corpus Engine is a good PyPI candidate, but this repository is not ready to publish as-is.**

The core is already a conventional pure-Python distribution: it uses a `src/` layout,
Hatchling, PEP 621 metadata, a console entry point, and versioned database migrations. A
wheel and source distribution both build, and a clean install of the built core wheel can
start `research-engine --help`.

The remaining work is not a packaging rewrite. It is a public-install contract:

1. make a checkout-independent install usable;
2. repair the standalone plugin SDK;
3. stop the default install from pulling the entire local ML/document stack;
4. add complete package metadata, installation documentation, and release automation;
5. close one public-facing plugin installation security gap.

**Recommended first public release:** `0.6.0`, after every P0 item below passes. The source
has substantial unversioned changes after the `0.5.0` changelog section, including migration
`018`, so publishing the current tree as `0.5.0` would reuse an identity that no longer
accurately describes it.

**Recommended names:** the MarginaliaAI family names `marginalia-ai` and
`marginalia-ai-sdk`. Both PyPI JSON endpoints returned 404 during this review, but that is
only a point-in-time availability check. A pending Trusted Publisher does not reserve a
name; only the first successful upload does.

## What was reviewed

- Root workspace metadata, both package `pyproject.toml` files, and the `uv` workspace.
- Core and SDK source layouts and public entry points.
- Built core and SDK wheels and source distributions.
- The installed CLI surface, migrations, configuration, database bootstrap, and runtime
  references to repository-only files.
- Unit/integration CI, changelog/version state, README installation instructions, and
  license handling.
- Plugin SDK imports, first-party pack imports, plugin dependency installation, permission
  handling, and documented security promises.
- Current PyPA packaging and PyPI Trusted Publishing guidance.

## Evidence from the current tree

| Check | Result | Meaning |
|---|---|---|
| `uv build --package marginalia-ai` | Built wheel and sdist successfully | Core build configuration is valid. |
| `uv build --package marginalia-ai-sdk` | Built wheel and sdist successfully | SDK build configuration is syntactically valid. |
| Core wheel size | 485,811 bytes | Project code itself is small and suitable for a universal wheel. |
| SDK wheel size | 1,733 bytes | This is a warning sign, confirmed by the import failure below. |
| `twine check` on all four artifacts | Passed, but warned that `long_description` and its content type are missing | PyPI would accept the files, but their project pages would have no useful README. |
| Core sdist contents | No `README.md` or `LICENSE` | Repository-level files are outside each package project root and are not being distributed. |
| SDK sdist contents | No `README.md` or `LICENSE` | Same defect as core. |
| Core wheel resources | `alembic.ini` and migration `018_anchor_editions.py` are present | Migration data is being included correctly. |
| Isolated core wheel install | Installed 156 packages and `research-engine --help` succeeded on Python 3.13 | The wheel is executable outside the checkout, but the default dependency set is excessive. |
| Isolated SDK wheel install | `from research_engine_sdk import tool` raised `ImportError` | The SDK must not be published in its current form. |
| PyPI JSON lookup | `marginalia-ai` and `marginalia-ai-sdk` both returned 404 | Names appeared unclaimed at review time; this is not a reservation. |
| Existing CI | Lint and unit/pack tests on Python 3.11, Ubuntu only | Good development baseline; insufficient release-artifact and compatibility coverage. |

The isolated core install downloaded the local inference stack, including Torch, Triton,
CUDA/cuDNN/NCCL wheels, sentence-transformers, Docling, OpenCV, SciPy, and related packages,
even though the only exercised operation was `--help`. That is the largest usability risk
for a normal `pip install`.

## Intended distribution model

This is a monorepo, but it does not contain one publishable project.

| Repository component | PyPI treatment | Public contract |
|---|---|---|
| `packages/core` | Publish as `marginalia-ai` | End-user CLI, MCP server, core services, migrations. |
| `packages/sdk` | Publish as `marginalia-ai-sdk` only after the SDK repair | Small, standalone plugin-author API with no dependency on core. |
| Root `marginalia-ai-workspace` | Never publish | `uv` development workspace only; `[tool.uv] package = false` already expresses this. |
| `packages/plugins/history` | Do not imply that it is in the core wheel | Keep repository-only for now, or release it separately after defining a remote install path. |
| `scripts/`, `tools/`, corpus setup material, screenshots, and `works/` | Do not include in the core wheel | Development/operations/corpus-specific assets, not general package runtime. |

The distribution name, import name, and command remain intentionally different forms:

- install: `pip install marginalia-ai`
- import: `import research_engine`
- run: `research-engine ...`

PyPI normalizes `.`, `_`, and `-`, so do not create aliases that differ only by those
characters.

## P0: release blockers

A release is not ready until every item in this section is closed.

### P0.1 — Expose installed migration commands

The migration-command blocker is resolved for the public distribution. Installed
users can manage packaged migrations without a Makefile or source tree:

```text
research-engine db current
research-engine db upgrade
research-engine doctor
```

The CLI resolves migrations from the installed `research_engine` package.
Schema changes remain explicit: runtime startup checks the Alembic head and
reports `research-engine db upgrade` when the database is behind, rather than
migrating implicitly. The unused `Settings.auto_migrate` field was removed, and
the development Makefile calls the same public command.

The end-user database guide covers:

- PostgreSQL 15 or newer;
- the server-side `vector`, `pg_trgm`, and `ltree` extensions;
- the fact that migration-created extensions may require an elevated database role;
- `RE_DB_URL` and credentials;
- running `research-engine db upgrade` and then `research-engine doctor`;
- `pg_dump` and `pg_restore` as external requirements for backup commands.

Remaining installation hardening:

- Choose a public bootstrap story. The conservative option is a documented Docker Compose
  example in the repository plus instructions for an existing Postgres server; the Python
  package should not attempt to install or own PostgreSQL.
- Replace the development database URL as a silent production default, or make the CLI
  report clearly that it is using a local development default. Requiring `RE_DB_URL` for
  non-development commands is safer.
- Revisit `.env` discovery for installed use. Outside a checkout there may be no enclosing
  `pyproject.toml`, so walking every parent can discover an unrelated `$HOME/.env`. Prefer
  explicit `RE_ENV_FILE`, a file in the invocation directory, or a documented platform
  configuration path.

Acceptance:

1. In a temporary directory with no checkout, install the built wheel.
2. Start a clean supported Postgres instance with the required extensions.
3. Set only documented environment variables.
4. Run the public upgrade command from empty database to migration head.
5. Run `research-engine status`, `research-engine doctor`, and a minimal MCP startup.

### P0.2 — Repair the standalone SDK and invert the dependency

`packages/sdk/src/research_engine_sdk/__init__.py` imports its API from
`research_engine.plugins.sdk` and catches `ImportError`. With core absent it exports nothing;
there are no standalone type stubs despite the comment saying otherwise. The observed wheel
fails its fundamental import:

```python
from research_engine_sdk import tool  # ImportError
```

The architecture documentation says plugin authors should import `research_engine_sdk`, but
the root README currently demonstrates `research_engine.plugins.sdk`. The first-party history
pack also directly imports a core domain type.

Implement a clean dependency direction:

- Move the actual public protocols, DTOs, decorators, and public plugin errors into
  `research_engine_sdk`.
- The SDK must not import `research_engine`, even conditionally.
- Make core depend on a compatible SDK release and import the shared contract from the SDK.
- Keep core implementations and adapters in core; only stable contracts belong in the SDK.
- Move or redefine every SDK-visible type that currently reaches into core domain modules.
  In particular, type annotations such as `DatePrecision`, `SearchQuery`, and `SearchResult`
  cannot remain unresolved references to core internals.
- Remove the broad `try/except ImportError`; missing SDK internals must fail during testing,
  not turn into an apparently successful empty package.
- Migrate all documentation and every first-party pack to
  `from research_engine_sdk import ...`.
- Define SDK compatibility explicitly. Since both projects release from one repository, the
  simplest policy through `0.x` is matching minor releases, for example core `0.6.x`
  depending on `marginalia-ai-sdk>=0.6,<0.7`.
- Add `py.typed` only if the shipped annotations are intentionally supported and checked.
  Do not claim `Typing :: Typed` merely because annotations exist.

Required SDK artifact test:

```python
import research_engine_sdk

for name in research_engine_sdk.__all__:
    assert getattr(research_engine_sdk, name) is not None
```

Run that in a clean environment where `marginalia-ai` is not installed. Then install core
against the same SDK wheel and run one real first-party pack contract scenario.

If the SDK repair is intentionally deferred, do **not** publish `marginalia-ai-sdk`, remove
claims that it is available, and mark third-party pack authoring unsupported for that core
release.

### P0.3 — Split heavyweight optional dependencies

`pip install marginalia-ai` currently resolves the complete local ML and document parsing
stack. The review's clean install pulled 156 packages, including several hundred-megabyte
GPU runtime wheels, just to display CLI help. This makes installation slow, platform-sensitive,
and surprising for users who use remote inference or do not ingest PDFs.

Recommended extras:

| Install | Purpose |
|---|---|
| `marginalia-ai` | CLI, database, MCP, configuration, remote inference clients, and lightweight formats. |
| `marginalia-ai[local-inference]` | `sentence-transformers` and its local model runtime. |
| `marginalia-ai[documents]` | Docling/PDF/EPUB/HTML parsing dependencies. |
| `marginalia-ai[embed-server]` | FastAPI and Uvicorn; document that local inference is also required. |
| `marginalia-ai[full]` | All supported optional features for users who want the current batteries-included behavior. |
| `marginalia-ai[dev]` | Contributor test/lint dependencies, if retaining a public dev extra is useful. |

Exact grouping should follow import boundaries, not only package size. Refactor eager imports
so base CLI startup does not import an unavailable optional backend. A missing extra must
produce one actionable error, for example:

```text
Local embedding support is not installed. Install marginalia-ai[local-inference]
or set RE_EMBEDDING_PROVIDER=remote_api.
```

Acceptance:

- A clean base install starts `--help`, `config`, database migration, and remote-provider
  operation without Torch, CUDA, sentence-transformers, or Docling in the environment.
- Each extra has a clean-environment feature smoke test.
- `full` exercises every registered ingestion module and local inference construction.
- CI verifies optional imports do not break unrelated commands.

### P0.4 — Complete and ship package metadata

Both artifacts currently lack a long description and both sdists omit the license text.
Create package-specific public READMEs at `packages/core/README.md` and
`packages/sdk/README.md`. Put an Apache-2.0 license file in each package project root and add
a CI equality check against the repository `LICENSE`, so portable copies do not drift.

Each package `pyproject.toml` needs:

- `readme = "README.md"`
- `license = "Apache-2.0"`
- `license-files = ["LICENSE"]`
- author/maintainer identity
- keywords
- tested Python classifiers
- development-status, console, audience, topic, and Apache license classifiers
- project URLs for source, issues, changelog, and documentation
- a minimum Hatchling version that supports the selected PEP 639 license metadata

Recommended core metadata shape:

```toml
[project]
name = "marginalia-ai"
dynamic = ["version"]
description = "Build a searchable, citable research corpus and expose it over MCP"
readme = "README.md"
requires-python = ">=3.11"
license = "Apache-2.0"
license-files = ["LICENSE"]
authors = [{ name = "MarginaliaAI" }]
keywords = ["corpus", "information-retrieval", "mcp", "research", "citations"]
classifiers = [
    "Development Status :: 4 - Beta",
    "Environment :: Console",
    "Intended Audience :: Science/Research",
    "License :: OSI Approved :: Apache Software License",
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.11",
    "Topic :: Text Processing :: Indexing",
]

[project.urls]
Homepage = "https://github.com/John-Cusack/MarginaliaAI"
Repository = "https://github.com/John-Cusack/MarginaliaAI"
Issues = "https://github.com/John-Cusack/MarginaliaAI/issues"
Changelog = "https://github.com/John-Cusack/MarginaliaAI/blob/main/CHANGELOG.md"

[build-system]
requires = ["hatchling>=1.26"]
build-backend = "hatchling.build"

[tool.hatch.version]
path = "src/research_engine/__init__.py"
```

Add Python 3.12, 3.13, and 3.14 classifiers only after their release-artifact jobs pass. Avoid
an operating-system classifier until the supported matrix is explicit.

The package READMEs must use PyPI-safe links. Include:

- what the project is and what `pip install` does not provide;
- base/full installation commands;
- system prerequisites;
- database creation and migration;
- minimal configuration;
- a real first command and MCP configuration example;
- optional feature extras;
- data sent to external LLM providers and potential API cost;
- local model download/disk/GPU expectations;
- plugin trust warning;
- links to full docs, issues, security reporting, changelog, and license.

`twine check --strict` must pass with no warning, and `License-File` plus the Markdown long
description must be present in wheel metadata.

### P0.5 — Add explicit consent before plugin code or setup execution

The pack design document promises that users see requested permissions and explicitly confirm
installation. The current CLI does neither. After cloning a repository, it can install declared
pip dependencies and execute manifest `setup_commands` with `shell=True`; the pack then runs
in the main process. Permission-gated clients are useful capability boundaries, but they are
not a sandbox against arbitrary Python code.

Before public release:

- Resolve and display the source URL, requested ref, resolved commit, pack identity,
  permissions, pip requirements, and every setup command.
- Ask for explicit confirmation before dependency installation, setup commands, database
  registration, or enabling the pack.
- Default to refusal in a non-interactive terminal; provide an explicit `--yes` only for
  trusted automation.
- Warn when installing a moving branch instead of a tag or commit SHA.
- Execute pip through the running interpreter's environment, not whichever unrelated `uv`
  environment happens to be discoverable.
- Document prominently that installed packs execute in-process and must be trusted.
- Make the public documentation match implemented permissions. Do not claim filesystem or
  network isolation beyond the actual gated clients.

This is a public distribution blocker because `pip install marginalia-ai` exposes the plugin
installer to users who did not inspect this repository's architecture notes.

### P0.6 — Remove checkout-only runtime instructions and false shipping claims

Installed runtime messages currently tell users to run:

- `scripts/backfill_words.py`
- `uv run python scripts/load_versification.py`

Those files are not in the wheel. The root README also says first-party packs “ship with the
engine,” while the core wheel contains only `research_engine` and not
`packages/plugins/history`.

For every runtime reference to `scripts/`, `tools/`, `packages/`, `Makefile`, or `uv run`:

- expose the operation as a supported CLI command; or
- move it into the pack that owns the domain-specific data; or
- replace the message with a stable documentation URL and state that it is a source-checkout
  maintenance procedure.

Recommended boundary:

- generic schema migration and repair operations become core CLI commands;
- Bible corpus construction and versification loading stay out of the general core package
  and move behind the relevant pack/operations documentation;
- the history pack is called repository-bundled, not installed/bundled with the wheel, until
  it has a real public install route.

Add an artifact-installed smoke test that runs from an empty temporary directory specifically
to catch accidental dependence on the checkout.

### P0.7 — Establish one release identity and accurate release notes

Current version state:

- core `pyproject.toml`: `0.5.0`
- `research_engine.__version__`: `0.5.0`
- SDK `pyproject.toml`: `0.1.0`
- changelog: many unversioned entries precede the `0.5.0` section
- actual migration head: `018_anchor_editions`
- root README and corpus setup guide still say migration head `017`

Implement:

- Single-source each distribution's version; do not hand-edit core metadata and
  `__version__` independently.
- Add an `[Unreleased]` changelog section and cut the current accumulated changes as
  `0.6.0`.
- Update all migration-head claims to `018` or, better, derive/display the head instead of
  copying it into prose.
- Tag policy is explicit: coordinated `v*` tags publish SDK, core, and history
  in dependency order; package-specific `core-v*` tags publish only core.
  Manual dispatch publishes one selected distribution to TestPyPI.
- Verify tag, package metadata, runtime version, and changelog version are identical before
  building.
- Document that PyPI files are immutable. A bad release is yanked and followed by a higher
  patch version; it is never rebuilt under the same version.

## P1: release-quality work

These items should be completed for the first public release unless the support statement is
narrowed explicitly.

### Compatibility matrix

The current CI tests only Python 3.11 on Ubuntu. The lockfile resolves markers for newer Python
versions and the wheel started on Python 3.13, but that is not a support matrix.

- Test supported Python versions with artifacts, not only the editable workspace.
- Start with Python 3.11–3.13. Add 3.14 after all selected extras resolve and their smoke tests
  pass.
- Test Ubuntu, macOS, and Windows if claiming cross-platform support. Otherwise document
  Linux as the initial supported runtime.
- Run at least one Postgres 15+ migration/integration job with pgvector.
- Exercise minimum supported direct dependencies periodically, plus a normal latest-resolver
  install. `uv.lock` protects contributors; it does not constrain what pip resolves for users.

### Public configuration and operations

- Add a generated or maintained configuration reference for all `RE_` settings, including
  defaults, secrets, and which feature consumes each value.
- State which commands require a database, local models, network, API credentials, Git, and
  PostgreSQL client binaries.
- Make `research-engine config` safe to paste into issues; secrets must remain redacted.
- Document model downloads, expected storage, CUDA/CPU behavior, outbound provider calls,
  and budget controls.
- Document backup/restore compatibility and migration expectations before upgrades.

### Security and support policy

- Add `SECURITY.md` with a private vulnerability-reporting path and supported versions.
- State that corpus passages sent for extraction leave the machine when a hosted LLM provider
  is configured.
- State that pack code is trusted in-process code, not sandboxed code.
- Keep credentials out of logs, diagnostics, examples, and issue templates.
- Enable PyPI account 2FA and retain recovery codes; have at least two recoverable maintainers
  if the project becomes operationally important.

### Changelog and compatibility policy

- Use SemVer/PEP 440-compatible versions.
- During `0.x`, state whether minor releases may break CLI, Python, MCP, SDK, pack manifest,
  or database contracts.
- Treat migration compatibility and pack `core_api` compatibility as explicit release notes.
- Record deprecations before removal; do not leave compatibility aliases indefinitely.
- Keep the SDK surface intentionally small. Core domain internals are not public merely
  because they are importable.

## Implementation sequence

The order matters because downstream work depends on the public contract chosen earlier.

### Phase A — public contract

- [ ] Distribution names are the MarginaliaAI family (`marginalia-ai`, `marginalia-ai-sdk`, `marginalia-ai-plugin-*`).
- [ ] Define initial supported OS/Python matrix.
- [ ] Decide whether SDK `0.6.x` releases in lockstep with core; default: yes.
- [ ] Define base install and optional extras.
- [ ] Define database bootstrap and explicit migration commands.
- [ ] State plugin trust and permission boundaries accurately.

### Phase B — code and packaging

- [ ] Move the standalone SDK contract into `packages/sdk` and invert core's dependency.
- [ ] Migrate core, first-party packs, tests, and docs to `research_engine_sdk` imports.
- [ ] Add installed-package database migration commands.
- [ ] Remove unused automatic-migration configuration.
- [ ] Split heavyweight optional dependencies and lazy-load their adapters.
- [ ] Add actionable missing-extra errors.
- [ ] Add plugin installation review/confirmation and safe non-interactive behavior.
- [ ] Eliminate runtime instructions that require unshipped repository files.
- [ ] Single-source versions.
- [ ] Add package READMEs and license files.
- [ ] Complete PEP 621 metadata and project URLs.
- [ ] Keep migrations and every required non-Python resource in the wheel.

### Phase C — documentation

- [ ] Replace the root quick start with separate “install from PyPI” and “develop from source” paths.
- [ ] Add the database/system-prerequisite guide.
- [ ] Add the full `RE_` configuration reference.
- [ ] Add optional feature and model-download guidance.
- [ ] Add privacy, cost, plugin trust, security, and support sections.
- [ ] Clarify what is and is not bundled: no database, corpus, models, external tools, or repository-only packs.
- [ ] Bring migration-head and pack-shipping claims current.
- [ ] Cut a structured `0.6.0` changelog section.

### Phase D — release verification

- [ ] Build core and SDK wheel plus sdist in isolated build environments.
- [ ] Run `twine check --strict` over exact artifacts.
- [ ] Inspect metadata for README, content type, URLs, license expression/file, dependencies, extras, Python requirement, and entry points.
- [ ] Verify artifact contents: migrations included; tests, caches, screenshots, corpora, credentials, and local work excluded.
- [ ] Install each wheel in a clean environment outside the checkout.
- [ ] Install each sdist in a second clean environment outside the checkout.
- [ ] Smoke-test core base commands and each extra.
- [ ] Import every SDK export without core installed.
- [ ] Upgrade an empty database to head from the installed wheel.
- [ ] Upgrade a copy of the prior release schema/data to head.
- [ ] Run a minimal ingest/search/verify/MCP scenario against the installed artifact.
- [ ] Test the declared Python/OS matrix.
- [ ] Confirm the tag, changelog, wheel metadata, runtime core version, SDK version, and pack compatibility ranges agree.

## Release automation

Add `.github/workflows/release.yml`. Keep building and publishing in separate jobs. The publish
job receives only previously tested artifacts and `id-token: write`; it does not check out or
execute repository build code.

Recommended job graph:

```text
release tag
  -> verify tag/changelog/version
  -> build SDK and core artifacts
  -> twine strict check + artifact inspection
  -> clean-wheel/sdist smoke tests + database smoke
  -> publish SDK with Trusted Publishing
  -> publish core with Trusted Publishing
  -> create/update GitHub Release with checksums and notes
```

SDK should publish before core if the new core requires that SDK version. Keep artifacts in
separate directories and use a separate publish job for each PyPI project. This prevents a
wildcard upload from accidentally publishing the workspace root or the wrong distribution.

Workflow requirements:

- Trigger only on protected release tags or a manually approved GitHub Release event.
- Use a protected GitHub Environment for each PyPI project, with required reviewer approval.
- Grant `id-token: write` only to the publish jobs.
- Use PyPI Trusted Publishing; store no long-lived PyPI token in GitHub.
- Pin third-party actions to reviewed commit SHAs; use Dependabot/Renovate to update pins.
- Upload/download the exact built artifacts between jobs.
- Keep default PEP 740 attestations enabled.
- Reject a version that already exists.
- Never use `skip-existing` for production publication; it can hide a partial or wrong release.

For the first release, configure a pending Trusted Publisher for each project using repository
owner `John-Cusack`, repository `MarginaliaAI`, workflow `release.yml`, and the exact protected
environment name used by the workflow. A pending publisher does not reserve a project name,
so configure it only when the release is ready and publish promptly.

TestPyPI is useful for checking project-page rendering and the OIDC path, but installation from
locally built exact artifacts is the stronger functional proof. TestPyPI has a separate account
and dependency index; do not treat a successful TestPyPI page as proof that normal PyPI
dependency resolution works.

## Repeatable release checklist

### Before tagging

- [ ] CI is green for the supported matrix.
- [ ] Integration/database migration smoke is green.
- [ ] No P0 item remains.
- [ ] Changelog has the release version and date.
- [ ] Package/runtime versions match the planned tag.
- [ ] Reinstall an editable package after changing its version
  (`uv sync --reinstall-package marginalia-ai`), then confirm
  `importlib.metadata.version("marginalia-ai")` matches
  `research_engine.__version__`.
- [ ] Pack `core_api` ranges are correct.
- [ ] Documentation shows the version's actual commands and migration head.
- [ ] PyPI names and Trusted Publisher configuration are valid.

### Build and inspect

```bash
rm -rf dist/core dist/sdk
uv build --package marginalia-ai-sdk --out-dir dist/sdk
uv build --package marginalia-ai --out-dir dist/core
uvx --from twine twine check --strict dist/sdk/* dist/core/*
```

Then run the artifact-install, SDK-import, extras, and database scenarios from Phase D. Do not
publish artifacts rebuilt after those checks; publish those exact files.

### Publish

- [ ] Create and push the protected version tag.
- [ ] Approve the protected PyPI environment after reviewing build provenance.
- [ ] Confirm SDK upload succeeds before core upload when core depends on it.
- [ ] Verify PyPI project pages, metadata, files, hashes, attestations, and rendered README.
- [ ] Install the exact version from production PyPI in a fresh environment.
- [ ] Run `research-engine --help`, the public migration command, and the minimal smoke scenario.
- [ ] Publish GitHub release notes and checksums.

### If a release is bad

- Stop promotion immediately.
- Yank the affected PyPI version if users should not select it.
- Do not delete/recreate or overwrite files; PyPI versions and filenames are immutable.
- Fix forward, increment the patch version, rebuild, rerun every gate, and publish a new release.
- For a database defect, publish explicit recovery instructions before asking users to upgrade.

## Go/no-go gate

Publish only when all statements below are true:

- `pip install marginalia-ai` has a documented, reasonably sized base install.
- A user with no checkout can provision/configure the database, migrate it, start the service,
  and understand every external prerequisite.
- `marginalia-ai-sdk` either works standalone and is tested, or is explicitly excluded from
  the release and documentation.
- Every runtime instruction names an installed command or stable public document.
- Package pages contain a useful README, source/support links, and the Apache license file.
- Plugin installation requires informed consent before executing third-party setup/code.
- Exact wheel and sdist artifacts pass clean-environment and database smoke tests.
- Versions, changelog, migrations, tags, compatibility ranges, and documentation agree.
- Publication uses protected, tokenless Trusted Publishing with attestations.

At that point this repository is not merely uploadable; it has a supportable `pip install`
contract.

## Authoritative references

- [PyPA packaging tutorial](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [PEP 621 `pyproject.toml` specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
- [PyPA GitHub Actions publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)
- [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/)
- [Creating a PyPI project with a pending Trusted Publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
- [PyPI publish action](https://github.com/pypa/gh-action-pypi-publish)
- [Hatch metadata configuration](https://hatch.pypa.io/latest/config/metadata/)
- [Hatch build configuration](https://hatch.pypa.io/latest/config/build/)
