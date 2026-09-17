# Research Engine History Plugin

The reference external-plugin implementation for Research Engine. It contributes
historical correspondence types, epistolary and claim extraction schemas, and two MCP tools:
`history.find_missing_letters` and `history.correspondence_cadence`.

## Install and approve

```bash
python -m pip install research-engine-plugin-history
research-engine plugin audit history
research-engine plugin enable history
```

Installation only makes the static manifest discoverable. Core does not import the package
until the operator approves the exact distribution version, manifest hash, contributions, and
permissions. The plugin runs in-process after approval and must be trusted.

This distribution depends only on `research-engine-sdk>=0.6,<0.7`. Integration environments
install the matching `research-engine` artifact separately.

See the [repository](https://github.com/John-Cusack/MarginaliaAI),
[changelog](https://github.com/John-Cusack/MarginaliaAI/blob/main/CHANGELOG.md), and
[issues](https://github.com/John-Cusack/MarginaliaAI/issues). Licensed under Apache-2.0.
