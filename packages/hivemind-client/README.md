# hivemind (client + CLI)

The Python client library and the `hivemind` CLI for a Hivemind server — the shared, versioned
knowledge graph + artifact store + tool registry that a fleet of agents reads and writes.

The CLI exists for the work that should not travel through a model: moving large artifacts over REST
(content-addressed, and verified on the way back), publishing and fetching self-contained tools, and
the agent bus.

```sh
export HIVEMIND_SERVER_URL=http://<host>:8787      # the server
export HIVEMIND_PROJECT=<project>                 # or pass --project <name>
export HIVEMIND_TOKEN=<token>
hivemind health
hivemind artifact put <file>                 # -> a sha256:… digest to attach
hivemind tool publish <script.py> --id <rdns> --version <semver>
```

- Requires Python ≥3.9, so it runs on a stock system interpreter with nothing installed.
  Dependencies: `httpx` and `websockets`, both pure-Python wheels.
- The URL is the server. Which project a command acts in comes from `--project <name>` or
  `$HIVEMIND_PROJECT`, and a command that reaches the graph or the blob store with neither is
  refused rather than defaulted into a project nobody named. A URL that already names one
  (`http://<host>:8787/p/<project>`) still works and needs no flag; `--project` overrides it.
- Those variables are how it is configured (or `--url`/`--token`/`--project`). Inside a Claude Code
  session with the Hivemind plugin installed, the plugin's `SessionStart` hook already exports the
  URL and token from the plugin's own config and `HIVEMIND_PROJECT` from the session pin — anything
  you export yourself takes precedence.
- In Codex, export the URL, token and project yourself for the CLI; its plugin does not transfer
  MCP configuration or project pins into shell processes. See the platform guides:
  [Codex](../../docs/codex-plugin.md) and [Claude Code](../../docs/user-guide.md).
- `hivemind health` reads `/healthz` off the server root, which takes no token: it answers `{"ok":
  true}` even with no project and a rejected token, so it is liveness only, never a check that the
  rest is configured.
- Source and documentation: <https://github.com/tsytsarkin/hivemind>
- Apache-2.0.
