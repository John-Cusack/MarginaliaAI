# Research Engine SDK

`research-engine-sdk` is the standalone, typed contract for building plugins for
[Research Engine](https://github.com/John-Cusack/MarginaliaAI). It contains static manifest
models, boundary DTOs, scoped client protocols, decorators, chunking helpers, and portable
contract tests. It does not install or import the Research Engine core.

## Install

```bash
python -m pip install "research-engine-sdk>=0.6,<0.7"
```

A plugin declares a static `plugin.yaml` inside its top-level import package and advertises
that package through the `research_engine.plugins` entry-point group. Plugin acquisition and
removal belong to pip, uv, or pipx; the running engine never installs plugin dependencies.

```toml
[project.entry-points."research_engine.plugins"]
example = "example"
```

```yaml
schema_version: 2
plugin_id: example
requires:
  core_api: ">=0.6,<0.7"
  python: ">=3.11"
permissions:
  network: none
  filesystem: plugin_data
provides:
  mcp_tools:
    - id: example.search
      entry: example.tools:search
      description: Search the example source.
      input_schema:
        type: object
        properties:
          query: {type: string}
        required: [query]
```

Plugin code executes in the core process after explicit operator approval. Scoped clients are
the supported API boundary, not a security sandbox; install and enable only trusted plugins.

API compatibility follows the core minor release through `0.x`. A plugin supporting core
`0.6.x` should depend on `research-engine-sdk>=0.6,<0.7`.

See the [architecture](https://github.com/John-Cusack/MarginaliaAI/blob/main/docs/design/pypi-plugin-distribution-architecture.md),
[changelog](https://github.com/John-Cusack/MarginaliaAI/blob/main/CHANGELOG.md), and
[issue tracker](https://github.com/John-Cusack/MarginaliaAI/issues). Licensed under
[Apache-2.0](https://github.com/John-Cusack/MarginaliaAI/blob/main/LICENSE).
