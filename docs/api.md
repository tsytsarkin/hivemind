# API surface

## MCP tools (over `/p/<project>/mcp`, 2026-07-28 streamable HTTP, Bearer auth)
Graph: `graph_search`, `graph_get`, `graph_subjects`, `graph_neighbors`, `graph_upsert`,
`graph_link`, `graph_bulk_load`. Schema: `schema_get`, `schema_propose`, `schema_promote`,
`schema_apply` (returns `{created, unchanged}`; idempotent). Artifacts: `artifact_ref`, `artifact_attach`, `artifact_refs`. Tools:
`tool_publish`, `tool_resolve`, `tool_search`, `tool_yank`. Guide: `guide_get`, `guide_propose`.
Read tools are annotated `readOnlyHint`; all return `{ok, …}` or `{ok:false, error, error_kind}`.

## REST (same prefix, same Bearer auth)
- `PUT/GET/HEAD /blobs/{algo}/{hex}` (streaming, Range, immutable) · `POST /blobs/batch` (LFS).
- `GET /guide` · `GET /guide/{section}` (ETag = guide_version).
- `GET /healthz`, `GET /projects` (server root, /healthz open).

## Clients
- `hivemind` CLI (`node/edge/search/neighbors/schema/artifact/tool/guide`; incl.
  `schema apply <pack.json>`), config from `HIVEMIND_SERVER_URL` + `HIVEMIND_TOKEN`.
- `hivemind.Client` (Python): `.call(tool, args)`, `.upsert/.get/.link/.search/.schema/.guide`,
  `.artifacts.put/get`, `.tool_publish/get/search`.
- `hivemind-admin` (operator, on the server host): `mint-token`, `create-project`, `apply-pack`,
  `promote`, `merge-guide`, `set-guide`, `gc`, `reindex`.
