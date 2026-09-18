# Hivemind — Muse plugin

Native Muse port of the Claude Code plugin in [`../plugin/`](../plugin/).
Same skill, same bootstrap script, same offline guide — repackaged as a native Muse
plugin (`.muse-plugin/plugin.json`, schemaVersion 1).

Ported from Claude plugin **v0.8.0**. The port deltas are:

- `SKILL.md` frontmatter: dropped Claude-only `allowed-tools` and `metadata` keys.
- The ``!`…guide.sh` `` auto-execute line (a Claude loader feature) became an explicit
  "run `scripts/guide.sh --section core`" instruction.
- No baked-in MCP server entry (see below): the endpoint URL and bearer token are
  per-deployment secrets, and the Muse manifest has no user-config templating or
  header field for HTTP servers (a `headers` key validates but is silently ignored —
  verified against the Muse 1.3 validator, so never put a token there).

`scripts/guide.sh` and `references/OFFLINE.md` are byte-identical to the Claude side.

## Layout

```text
muse-plugin/
  .muse-plugin/plugin.json      # native manifest (skills capability only)
  skills/hivemind/SKILL.md      # the skill
  skills/hivemind/scripts/guide.sh
  skills/hivemind/references/OFFLINE.md
```

## Install

```sh
muse plugins install /path/to/hivemind/muse-plugin
muse plugins list               # verify it is installed
```

## Connect the MCP tools (per user — never commit this part)

The skill's MCP tools (`graph_*`, `schema_*`, `guide_*`, …) need a streamable-HTTP
entry for your Hivemind project in your own Muse `settings.json`
(`$XDG_CONFIG_HOME/muse/settings.json`, else `$HOME/.config/muse/settings.json`):

- URL: `<HIVEMIND_SERVER_URL>/mcp` (e.g. `http://<server-host>:8787/p/default/mcp`)
- Header: `Authorization: Bearer <HIVEMIND_TOKEN>` (mint on the server host —
  see `../deploy/DEPLOY.md`)

Use whatever field names your Muse version documents for HTTP server entries under
`mcpServers`. (`muse mcp login` is OAuth-only and does not apply to Hivemind's
static bearer token.) Takes effect on the next Muse launch.

Without the MCP entry the skill still works in degraded mode: `guide.sh` and the
`hivemind` CLI talk REST directly once `HIVEMIND_SERVER_URL` + `HIVEMIND_TOKEN` are
exported (see `SKILL.md`).

## Validate

```sh
muse skills validate muse-plugin/skills/hivemind --json
muse plugins validate muse-plugin --json
```

Both must report `"valid": true` with empty `"diagnostics"`.
