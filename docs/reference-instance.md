# Running a profiled reference instance over ssh-stdio

A reference instance is a separate `research-engine serve` with its own
database and env file, exposing only the read-only tools of
`RE_MCP_PROFILE=reference`. Agents on other machines reach it as MCP over stdio,
tunnelled through ssh.

## Set the profile in the launching command, not only in the env file

`RE_MCP_PROFILE` turns on the deployment guard: settings refuse a `.env` found
by walking up from the working directory, a `RE_ENV_FILE` that names no file,
and the built-in development `RE_DB_URL`. The guard can only act on a profile it
can see. A profile written only inside the env file disappears with that file:
if `RE_ENV_FILE` is wrong or the file has moved, the profile never loads, the
guard stays off, and `serve` starts with every tool on the development database.

So the command the ssh client runs exports both variables itself. Keep the
instance's `RE_DB_URL` and other settings in the env file:

```sh
#!/bin/sh
export RE_ENV_FILE="$HOME/.config/marginalia-eng/env"
export RE_MCP_PROFILE=reference
exec "$HOME/srv/marginalia-eng/app/.venv/bin/research-engine" serve
```

If the env file is missing, this exits non-zero with a `ConfigurationError`
naming `RE_ENV_FILE`. It does not open a database.

The client's MCP entry runs that wrapper over ssh, for example
`ssh -T tower /home/<user>/srv/marginalia-eng/bin/serve-reference`.
`RE_MCP_TOOLS` (comma-separated) can narrow the profile further. It can never
widen it, and leaving it blank means no extra narrowing.
