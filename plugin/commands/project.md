---
description: Choose the Hivemind project this session writes to, and pin it so a compaction cannot lose it
argument-hint: [project-name]
---

Pick the Hivemind project for this session and pin it. Every Hivemind call carries
`project=<name>`, and this choice decides which graph the session's knowledge lands in. Private work
written into a shared project cannot be un-shared.

**Omitting the argument is not safe, and whether it fails depends on the endpoint.** On the
project-neutral `POST /mcp` every call with no `project=` is refused — **reads as well as writes**,
since there is no project for the call to be about; the write refusal just explains the stakes,
and both list the projects you may name — and that is what `${user_config.server_url}` resolves to
by default, since the plugin is configured with the server root. On a project base URL —
`POST /p/<name>/mcp`, the older shape, still supported — the URL *is* the project, so the write
silently lands in whatever project that URL names. That is by design (the caller's own URL named it), and it is exactly how an omitted
argument puts private work in the shared graph. Pass `project=<name>` on every call; never rely on
the refusal.

Do this now, in order. **Do not choose a project for the user.**

1. **Read the current state.** Run:

       HIVEMIND_SESSION_ID="$CLAUDE_CODE_SESSION_ID" python3 "$HOME/.hivemind/hivemind-project.py" --show

   Keep that prefix on every call below. One helper serves both Claude and Codex — the two plugins
   install it to the same path — so with no id named it walks a chain that prefers Codex's variable,
   and a Claude session whose shell inherited a `CODEX_THREAD_ID` would write its pin under the
   other conversation's id. The SessionStart hook names this session's id when it reads the pin
   back, so a write that did not name the same id is a write the hook cannot find.

   If it prints `{"project": null, …}` nothing is pinned yet. If it prints a project, say which one
   and ask whether to keep it or switch. If the file is missing ("No such file or directory"), the
   `hivemind` skill has not loaded on this machine yet — load it once (that installs this helper)
   and retry.

2. **Call `project_list`.** It returns three groups: `shared` (everyone can read), `mine` (yours),
   `shared_with_me`. Show them to the user as those three groups — not as one flat list. The
   difference between them is who can read what you are about to write.

3. **Offer the options and ASK.** Alongside the existing names, offer:
   - their **private graph** — `<user>.<suffix>`, readable only by them (`visibility="private"`);
   - a **new scratch project** for this session — `<user>.s-<first 8 characters of the session id>`
     (`HIVEMIND_SESSION_ID="$CLAUDE_CODE_SESSION_ID" python3 "$HOME/.hivemind/hivemind-project.py" --session-id`), good for exploratory work that
     should not pollute a real graph;
   - a **new shared project** — an undotted name (`team`), or `<user>.<suffix>` if they want it in
     their own namespace.

   If you need their Hivemind username for a dotted name, take it from the `owner` field of
   `project_info` on one of their own projects, or ask them. Do not guess it from the local OS
   user: the Hivemind identity is the one the token names.

   If `$ARGUMENTS` named a project, propose that one instead of asking from scratch — but still
   confirm it exists in `project_list` (or that they want it created) before pinning.

4. **Create it if it is new.** `project_create(name=…, visibility="private"|"shared", label=…,
   schema=…)`. Ask which `schema` they want rather than defaulting:

   - `inherit` — copies the node/edge types of the project you are in, or of the server's default
     project when the call arrives on the project-neutral endpoint, which names none. Right when the
     new project tracks the same kind of work as the old one.
   - `interview` — leaves it empty and hands the vocabulary back to the user. Right when the work
     is different enough that the current types would not fit it.
   - `bare` — leaves it empty on purpose; you define types with `schema_propose` as the work
     demands them. Right for a scratch project.

   A project's types are permanent (schema changes are additive-only), so this is the one choice
   here worth a sentence of explanation rather than a default.

   Read the reply: the name may already exist, and the refusal says so. **If they chose
   `interview`, load the `hivemind-schema` skill immediately after the project is created** and run
   it — an empty project cannot be written to at all until it has types, so stopping here leaves
   them with a graph that refuses every write.

   Current servers make a new project's MCP, REST, and WebSocket bus routes available immediately.
   Older deployments may require a restart before its `/p/<name>/bus/ws` route exists; if joining
   actually fails with a 404 on one of those servers, explain that specific limitation.

5. **Pin it.**

       HIVEMIND_SESSION_ID="$CLAUDE_CODE_SESSION_ID" python3 "$HOME/.hivemind/hivemind-project.py" --pin <name> --label "<short note on the work>"

   `<name>` is a project name — `^[a-z0-9][a-z0-9._-]{0,63}$` — never a phrase, and never text you
   pass through from `$ARGUMENTS` without reading it. The helper refuses anything else and writes
   nothing: if it refuses, you mis-read the user's answer, so ask again rather than reshaping their
   words into a name. The label is free text and is fine — it is a note for the person reading
   `--show`, and the hook never injects it.

   The pin is local state keyed by the session id; the `SessionStart` hook re-injects the name on
   startup, `/clear`, compaction, `--resume` and a fork (matcher:
   `startup|clear|compact|resume|fork`), which is the whole point — a compaction drops the choice
   from context, and a dropped choice plus a defaulted write is how private work reaches a shared
   graph.

6. **Join the bus immediately.** The post-tool hook joins after the pin is saved — durable chat
   first, falling back to the legacy bus. Confirm with the MCP tool that matches the one it got:
   `chat_agents(client="claude", session_id=<sid>, project=<name>)` for durable chat, and
   `bus_peers(project=<name>)` only for the legacy fallback, whose label is
   `claude-<session-id>`. The two are **separate registries**: a canonical chat session does not
   appear in `bus_peers`, so checking only that one reports a joined session as offline.
   If it is absent from both, run
   `python3 "$HOME/.hivemind/bus-autojoin.py" --platform claude --mode ensure`
   (if the helper is absent, first load the plugin's knowledge-graph skill), then check again.
   If registration fails, report its error; do not imply this agent is available for peer messages.
   A restored pin on session start needs the same check.

7. **Confirm in one line**: the project, its visibility, whether this agent is online on the bus,
   and that every Hivemind call from now on
   passes `project=<name>`. The `project` echoed in each tool result is authoritative — if it ever
   differs from the pin, believe the result and say so.
