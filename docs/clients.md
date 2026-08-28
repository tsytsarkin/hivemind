# Adding a machine (plugin + token)

Hivemind is **one server, many clients**. Don't run a second server per machine — each server has
its own SQLite database and blob store, so a second instance is a *separate graph*, not a shared
one. Point every machine at the same server URL.

## 1. Is the server reachable on your LAN?

The server binds **`127.0.0.1` by default (localhost only)**. To serve other machines set:

```sh
HIVEMIND_HOST=0.0.0.0        # listen on all private interfaces (LAN / Tailscale)
HIVEMIND_PORT=8787
```
(That's what `deploy/hivemind.env` does.) Verify from another machine:
```sh
curl http://<server-ip>:8787/healthz              # server root
curl http://<server-ip>:8787/p/default/healthz    # the project base your clients use
curl http://<server-ip>:8787/p/default/           # index of every endpoint for that project
```
Both health paths and both indexes are **open** (no token) so a probe works with only a URL;
everything else returns `401` without a bearer token.
**Being on the LAN is not authorization** — every `/p/<project>` request still needs a bearer
token; unauthenticated requests get `401`. `HIVEMIND_ALLOWED_HOSTS=*` disables the DNS-rebinding
host check (fine on a trusted private network); set explicit hostnames to enable it.

## 2. Mint a token for the new machine

Run **on the server host**, one token per machine so you can revoke individually:

```sh
hivemind-admin --project default mint-token --client-id <machine-name>
# -> {"token": "hm_…", …}   (shown once; it is stored in the project's tokens.json)
```

**Transferring it:** the token is a bearer credential — move it over a channel you already trust:

```sh
# simplest: mint it over ssh and capture it directly on the client machine
ssh you@server 'hivemind-admin --project default mint-token --client-id laptop' | tee ~/hm-token.json
```
or copy/paste from an SSH session into the machine's password manager / Keychain. **Don't** send it
over chat or email, and don't commit it. To revoke, delete that entry from
`<data-dir>/projects/<project>/tokens.json` on the server and restart.

## 3. Install just the plugin on the new machine

The plugin is self-contained (a manifest, an `.mcp.json`, and a skill) — the machine needs
**no server, no Python, no repo checkout**.

```sh
# from the git repo (works anywhere the machine can reach the repo):
claude plugin marketplace add tsytsarkin/hivemind        # or: <git-url>
claude plugin install hivemind@hivemind-marketplace --scope user \
  --config server_url=http://<server-ip>:8787/p/default \
  --config api_token=hm_…
```

If the repo is **private** and the machine has no GitHub credentials, use either:
```sh
# a) clone once with your own auth, then add the local checkout
git clone git@github.com:tsytsarkin/hivemind.git && claude plugin marketplace add ./hivemind

# b) copy just the plugin directory over ssh (it is a few KB)
scp -r you@thismachine:~/hivemind/plugin  ~/hivemind-plugin
claude plugin marketplace add ~/hivemind-plugin   # add a .claude-plugin/marketplace.json alongside,
                                                  # or point marketplace add at a repo root copy
```

Verify the connection:
```sh
claude mcp list
# plugin:hivemind:hivemind: http://<server-ip>:8787/p/default/mcp (HTTP) - ✔ Connected
```
`api_token` is declared `sensitive`, so Claude Code stores it in the OS keychain rather than in a
settings file.

## 4. (Optional) the CLI, for large artifacts and tool publishing

The plugin covers in-conversation use. For big uploads/downloads and publishing tools, install the
client too (Python ≥3.9, only dep is `httpx`) — see [DEPLOY.md](../deploy/DEPLOY.md):
```sh
pip install ./packages/hivemind-client
export HIVEMIND_SERVER_URL=http://<server-ip>:8787/p/default
export HIVEMIND_TOKEN=hm_…
hivemind health
```
