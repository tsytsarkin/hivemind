# hivemind-server

The Hivemind server: a shared, versioned knowledge graph + content-addressed artifact store + tool
registry, served over MCP (streamable HTTP) and REST and backed by SQLite. It is domain-agnostic —
node and edge **types are defined at runtime in the schema**, never compiled in.

One server holds many **projects**: separate graphs, some shared with every user, some private to
one. Every MCP tool takes a `project=<name>` argument, and a call that resolves no project is
refused rather than defaulted. Tokens name a person, and every write records its author.

- Requires Python ≥3.11.
- Console scripts: `hivemind-server` (the service) and `hivemind-admin` (operator CLI).
- Installing, deploying, backing up and restoring: `deploy/DEPLOY.md` in the repository.
- Agent usage guides: [Claude Code](../../docs/user-guide.md) and
  [Codex](../../docs/codex-plugin.md).
- Source and documentation: <https://github.com/tsytsarkin/hivemind>
- Apache-2.0.
