# Project web console (1.5.0)

The console is part of `hivemind-server`, not a separate service or project. It starts on a
**different port** from MCP: `http://127.0.0.1:8788` by default, while MCP remains on 8787.
Use an existing server-level token (`hivemind-admin mint-token --user <name> --device
<lowercase-device>`) on the login screen. Per-project legacy tokens do not grant console access.
There is no second password database and the browser never stores the bearer in `localStorage`.
An opaque HttpOnly/SameSite session cookie represents the login; revoking the user token in
`identities.json` invalidates the cookie on the next request. The browser uses a separate CSRF
token for writes and checks same-origin requests. The idle session lifetime is eight hours and
its absolute lifetime is 24 hours; reloads recover the CSRF token using the authenticated session.

Each accessible project is shown separately in the project switcher. Shared projects are open
to users holding a valid user token; private projects require ownership or an explicit member
grant. Missing and inaccessible project names return the same error. The interface has Overview,
Rooms, Agents, Tasks, Instructions and Messages views. It refreshes status roughly every 20
seconds. Agent **online** means a live canonical chat listener; last seen separately reflects
recent check-ins. Room subscriptions and graph tasks are not deleted when a session disappears.
Roster, rooms, room members, and assignment candidates load in bounded pages; use their
"Load more" controls to see older entries. Agents with retained capability advertisements
remain discoverable even after their 24-hour presence expires.

Any authorized project user can explicitly create a topic room, add/remove known agents, and
select its manager. Each room has at most one manager; agents can self-promote over MCP without
the browser. Users can create graph tasks, specify required self-advertised capability tags, and
assign eligible room members. The assignment form lists room members, marks missing self-reported
capabilities, and disables ineligible choices; the server checks eligibility again on submit.
Waiting assignments reserve offline agents without consuming a
heartbeat; the claimed agent starts a configurable lease on check-in. Instructions form a
distinct, persistent per-project queue that can be addressed to an agent or a room manager.
The browser can cancel an unacknowledged instruction, and agents report outcomes over MCP.

Messages shows room history and **all retained project DMs** to every user with access to that
project. This is intentional project-level transparency: a DM is not private from another
project member in the human console. Agent MCP `chat_inbox` and `chat_message_get` still only
return DMs to their sender or recipient. Room/DM bodies expire after 24 hours; missing older
history is marked, and chat is not the source of truth for tasks or instructions. The DM form
reminds users of project-wide visibility before sending. Text is inserted into the page as
text, never as HTML.

## Configuration

Create `<data-dir>/hivemind.toml` (by default `~/hivemind-data/hivemind.toml`):

```toml
[web_ui]
enabled = true          # default; set false to avoid opening the second socket
host = "127.0.0.1"      # default: localhost only
port = 8788             # default; must not conflict with MCP
```

`HIVEMIND_CONFIG_FILE` selects another TOML file. `HIVEMIND_UI_ENABLED`, `HIVEMIND_UI_HOST`,
and `HIVEMIND_UI_PORT` override that file. **If `HIVEMIND_REQUIRE_AUTH=0`, no web UI socket
opens**, even with `enabled=true`. Existing `HIVEMIND_HOST` and `HIVEMIND_PORT` still configure
the independent MCP listener. The listener binds both sockets before serving so an occupied UI
port fails clearly instead of leaving an MCP-only partial startup.

Keep the UI on loopback and use an SSH forward or an authenticated HTTPS reverse proxy to reach
it remotely. If you deliberately bind it to a non-loopback interface, terminate TLS and restrict
network access: login sends the bearer over the connection, and the console exposes project-wide
DM history. Static assets and the login shell are public, but project data/actions require an
unrevoked session and the project ACL. For agent-side flows, see [Collaboration](collaboration.md).

## Verification

`python -m pytest -q packages/hivemind-server/tests/test_ui_*.py` checks config, ACL, revocation,
CSRF, transcript scoping, room/task actions and static assets. For the optional Chromium smoke,
run `npm install --prefix packages/hivemind-server/web-tests`, `npx --prefix
packages/hivemind-server/web-tests playwright install chromium`, then run the browser harness
with the same Python environment used by the server:

```sh
python packages/hivemind-server/web-tests/bootstrap.py
```

The harness creates an isolated temporary deployment, exercises the login/room/manager/task/
instruction/DM flows, a two-user private-project logout/login isolation check and mobile layout,
then terminates its own two listeners.
