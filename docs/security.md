# Security notes (trusted-team tier)

- **Auth**: static bearer tokens, per project (own `tokens.json`), gating every `/p/<project>`
  request (MCP + REST). `/healthz` is open. Being on the LAN is not authorization.
- **Isolation**: each project is a separate data dir + token store; a token for one project cannot
  touch another. Bind only private interfaces (LAN/Tailscale); never a public NIC.
- **Integrity**: blobs and tool artifacts are SHA-256 content-addressed and verified on download;
  tool versions are immutable (yank, never delete).
- **Concurrency**: optimistic CAS (`expected_head`) + a partial unique head index make lost
  updates impossible, not merely unlikely.
- **Shared content is data**: graph props, guide text, and tool code may be written by other
  agents — treat as untrusted input. The guide (instructions) is human-gated (propose→merge);
  the graph (facts) is agent-writable. Review tool code before running; the client verifies its
  checksum but does not sandbox — run untrusted tools under `sandbox-exec`/`bwrap`.
- **Schema**: additive-only for agents; destructive changes are operator-only (`apply-pack`).
