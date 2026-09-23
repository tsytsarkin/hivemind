# hivemind (client + CLI)

The Python client library and the `hivemind` CLI for a Hivemind server — the shared, versioned
knowledge graph + artifact store + tool registry that a fleet of agents reads and writes.

The CLI exists for the work that should not travel through a model: moving large artifacts over REST
(content-addressed, and verified on the way back), publishing and fetching self-contained tools, and
the agent bus.

```sh
export HIVEMIND_SERVER_URL=http://<host>:8787/p/<project>
export HIVEMIND_TOKEN=<token>
hivemind health
hivemind artifact put <file>                 # -> a sha256:… digest to attach
hivemind tool publish <script.py> --id <rdns> --version <semver>
```

- Requires Python ≥3.9, so it runs on a stock system interpreter with nothing installed.
  Dependencies: `httpx` and `websockets`, both pure-Python wheels.
- The URL must name a project: the CLI has no `--project` flag and acts in whatever project its URL
  names.
- Source and documentation: <https://github.com/tsytsarkin/hivemind>
- Apache-2.0.
