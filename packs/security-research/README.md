# security-research pack

Example domain vocabulary for Hivemind (iOS/macOS vuln research). Nothing here is part of the
engine — it is loaded as data and is fully swappable.

Apply it to a project:

```sh
# on the server host (operator):
hivemind-admin --project default apply-pack packs/security-research/schema.json
# this also loads packs/security-research/guide/*.md as guide sections

# or remotely via the client (trusted-team):
hivemind schema apply packs/security-research/schema.json
```

Then `schema_get` shows the types and `guide_get("domain")` shows the workflow. To model a
different domain, write your own `schema.json` + `guide/` and apply it instead.
