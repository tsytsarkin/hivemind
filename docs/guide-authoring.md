# Authoring the live guide + domain packs

The **guide** is how domain knowledge reaches agents at runtime (the on-disk skill stays tiny).

- Sections are markdown in `guide_section`, budget-capped (~5k tokens). `core` is seeded with the
  framework guide. Add domain sections via a pack's `guide/*.md` or `hivemind-admin set-guide`.
- Agents call `guide_get()` / `guide_get(section)`; in Claude Code the skill also
  dynamic-injects `core` via `guide.sh`, while in Codex the agent runs that helper explicitly
  when needed (best-effort, never fails). That helper is the one guide path that needs the
  **environment**: a token, plus enough to build `/p/<project>/guide/<section>`, which is where the
  REST guide is mounted. It takes a URL that already contains `/p/` verbatim, and otherwise composes
  the prefix itself from `HIVEMIND_PROJECT` — or, when that is unset, from the session pin file it
  re-reads on each call, so a mid-session project switch is followed. The Claude plugin config
  holds no project at all (`plugin.json` declares only `server_url` and `api_token`); its
  `SessionStart` hook exports the first two and takes `HIVEMIND_PROJECT` from the pin. The Codex
  plugin requires the URL and token in the environment for shell use; see
  [Codex setup](codex-plugin.md). When any of
  that is missing the helper prints a cached or bundled copy and names the cause it hit — no
  URL/token in the shell, or no project for the root URL (which tells you to run
  `/hivemind:project` in Claude or the `hivemind-project` skill in Codex rather than sending you
  after a network fault). `guide_get` over MCP needs
  none of it.
- **Firewall**: agents `guide_propose`; an operator `hivemind-admin merge-guide <id>` publishes it,
  bumping `guide_version`. Keep instructions out of the agent-writable graph.

A **domain pack** is `schema.json` (`node_types`, `edge_types` with traits) + optional `guide/*.md`:
```sh
hivemind-admin --project <p> apply-pack packs/<yourpack>/schema.json   # loads schema + guide/*.md
```
Swap the pack to change the domain; the engine is unchanged. See [packs.md](packs.md) for details on
**layering** (apply several packs to one project — they compose additively) and the **idempotent**
re-apply (unchanged types are skipped, so re-running a pack on every deploy is safe).
