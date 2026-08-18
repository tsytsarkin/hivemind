-- Hivemind core schema. SQLite. Domain-agnostic: node/edge *types* live in node_type/edge_type
-- as data; the engine hardcodes no relationship types.
--
-- SENTINEL for "current" (open) tx_to = 9223372036854775807 (max signed int64).

-- ── provenance ────────────────────────────────────────────────────────────────
-- One row per write. tx_id is the monotonic "as-of" coordinate for the revision axis.
CREATE TABLE IF NOT EXISTS tx (
  tx_id    INTEGER PRIMARY KEY,
  tx_time  TEXT    NOT NULL,                              -- ISO-8601 UTC
  agent_id TEXT    NOT NULL,                              -- who wrote it
  reason   TEXT,                                          -- free-text note
  meta     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(meta))
);

-- ── schema-as-data (never run DDL at runtime) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS node_type (
  name        TEXT    NOT NULL,
  version     INTEGER NOT NULL,
  json_schema TEXT    NOT NULL CHECK (json_valid(json_schema)),  -- JSON Schema 2020-12 for props
  status      TEXT    NOT NULL DEFAULT 'proposed',                -- proposed | active | deprecated
  parent      TEXT,                                                -- optional single-inheritance
  created_tx  INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (name, version)
);

-- edge_type carries GENERIC behavioral traits; the engine special-cases none of them by name.
CREATE TABLE IF NOT EXISTS edge_type (
  name        TEXT    NOT NULL,
  version     INTEGER NOT NULL,
  json_schema TEXT    NOT NULL DEFAULT '{"type":"object"}' CHECK (json_valid(json_schema)),
  src_types   TEXT    NOT NULL DEFAULT '["*"]' CHECK (json_valid(src_types)),  -- domain ('*'=any)
  dst_types   TEXT    NOT NULL DEFAULT '["*"]' CHECK (json_valid(dst_types)),  -- range  ('*'=any)
  cardinality TEXT    NOT NULL DEFAULT 'N:N',            -- 1:1 | 1:N | N:N
  directed    INTEGER NOT NULL DEFAULT 1,
  symmetric   INTEGER NOT NULL DEFAULT 0,
  transitive  INTEGER NOT NULL DEFAULT 0,
  acyclic     INTEGER NOT NULL DEFAULT 0,                -- reject a cycle-forming insert
  versioned   INTEGER NOT NULL DEFAULT 1,                -- 0 = bulk edge (edge_bulk), no history
  assertive   INTEGER NOT NULL DEFAULT 0,                -- edges carry props.status; open ones surfaced
  status      TEXT    NOT NULL DEFAULT 'proposed',
  created_tx  INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (name, version)
);

-- ── stable identities ───────────────────────────────────────────────────────────
-- subject_* = the SUBJECT-VERSION axis (opaque to the engine):
--   subject_key     = stable id of the described thing; NULL = not subject-versioned
--   subject_version = version coordinate of that thing (e.g. "26.6", a build tag, a binary sha)
--   subject_order   = optional sortable key for latest/as-of over subject_version
CREATE TABLE IF NOT EXISTS node (
  node_id         TEXT PRIMARY KEY,
  node_type       TEXT NOT NULL,
  subject_key     TEXT,
  subject_version TEXT,
  subject_order   TEXT,
  redirect_to     TEXT REFERENCES node(node_id),         -- cross-identity merge tombstone (built-in)
  created_tx      INTEGER NOT NULL REFERENCES tx(tx_id)
);
-- one node per (subject_key, subject_version) cell → upsert-by-subject is deterministic
CREATE UNIQUE INDEX IF NOT EXISTS ux_subject
  ON node(subject_key, subject_version) WHERE subject_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_subject_order ON node(subject_key, subject_order);
CREATE INDEX IF NOT EXISTS ix_node_type ON node(node_type);

CREATE TABLE IF NOT EXISTS edge (
  edge_id     TEXT PRIMARY KEY,
  edge_type   TEXT NOT NULL,
  src_node_id TEXT NOT NULL REFERENCES node(node_id),
  dst_node_id TEXT NOT NULL REFERENCES node(node_id),
  created_tx  INTEGER NOT NULL REFERENCES tx(tx_id)
);
CREATE INDEX IF NOT EXISTS ix_edge_src ON edge(src_node_id, edge_type);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON edge(dst_node_id, edge_type);

-- ── immutable versions (revision axis) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS node_version (
  version_id   TEXT PRIMARY KEY,
  node_id      TEXT NOT NULL REFERENCES node(node_id),
  seq          INTEGER NOT NULL,
  prev_version TEXT REFERENCES node_version(version_id),
  props        TEXT NOT NULL CHECK (json_valid(props)),
  schema_ver   INTEGER NOT NULL,                         -- which node_type version validated this
  content_hash TEXT NOT NULL,                            -- sha256(canonical json) — dedup + idempotency
  tx_from      INTEGER NOT NULL REFERENCES tx(tx_id),
  tx_to        INTEGER NOT NULL DEFAULT 9223372036854775807,
  retracted    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (node_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_node_head
  ON node_version(node_id) WHERE tx_to = 9223372036854775807;
CREATE INDEX IF NOT EXISTS ix_node_asof ON node_version(node_id, tx_from, tx_to);
CREATE INDEX IF NOT EXISTS ix_node_ver_hash ON node_version(content_hash);

CREATE TABLE IF NOT EXISTS edge_version (
  version_id   TEXT PRIMARY KEY,
  edge_id      TEXT NOT NULL REFERENCES edge(edge_id),
  seq          INTEGER NOT NULL,
  prev_version TEXT REFERENCES edge_version(version_id),
  props        TEXT NOT NULL CHECK (json_valid(props)),
  schema_ver   INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  tx_from      INTEGER NOT NULL REFERENCES tx(tx_id),
  tx_to        INTEGER NOT NULL DEFAULT 9223372036854775807,
  retracted    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (edge_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_edge_head
  ON edge_version(edge_id) WHERE tx_to = 9223372036854775807;
CREATE INDEX IF NOT EXISTS ix_edge_asof ON edge_version(edge_id, tx_from, tx_to);

-- ── bulk edges (versioned=0): high-volume imported graphs, no per-edge history ───
CREATE TABLE IF NOT EXISTS edge_bulk (
  edge_type   TEXT NOT NULL,
  src_node_id TEXT NOT NULL,
  dst_node_id TEXT NOT NULL,
  props       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(props)),
  source_tag  TEXT NOT NULL,                             -- e.g. "kernelcache@26A5388g"; replace-by-tag
  created_tx  INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (edge_type, src_node_id, dst_node_id, source_tag)
);
CREATE INDEX IF NOT EXISTS ix_bulk_src ON edge_bulk(src_node_id, edge_type);
CREATE INDEX IF NOT EXISTS ix_bulk_dst ON edge_bulk(dst_node_id, edge_type);

-- ── content-addressed blobs (bytes on disk; rows are references) ─────────────────
CREATE TABLE IF NOT EXISTS blob (
  digest     TEXT PRIMARY KEY,                           -- 'sha256:<hex>'
  size       INTEGER NOT NULL,
  media_type TEXT,
  created_tx INTEGER NOT NULL REFERENCES tx(tx_id)
);
CREATE TABLE IF NOT EXISTS blob_ref (                    -- reachability, NOT a refcount
  digest          TEXT NOT NULL REFERENCES blob(digest),
  from_version_id TEXT NOT NULL,                         -- a node_version or edge_version id
  role            TEXT NOT NULL DEFAULT 'attachment',
  filename        TEXT,
  PRIMARY KEY (digest, from_version_id, role)
);
CREATE INDEX IF NOT EXISTS ix_blob_ref_from ON blob_ref(from_version_id);
CREATE TABLE IF NOT EXISTS blob_pin (                    -- GC roots
  digest TEXT PRIMARY KEY REFERENCES blob(digest),
  reason TEXT
);

-- ── tool registry (immutable published versions) ────────────────────────────────
CREATE TABLE IF NOT EXISTS tool (
  id             TEXT PRIMARY KEY,                        -- reverse-DNS
  latest_version TEXT,
  created_tx     INTEGER NOT NULL REFERENCES tx(tx_id)
);
CREATE TABLE IF NOT EXISTS tool_version (
  id              TEXT NOT NULL REFERENCES tool(id),
  version         TEXT NOT NULL,                          -- semver, immutable
  manifest        TEXT NOT NULL CHECK (json_valid(manifest)),
  artifact_digest TEXT REFERENCES blob(digest),
  yanked          INTEGER NOT NULL DEFAULT 0,
  yanked_reason   TEXT,
  created_tx      INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (id, version)
);

-- ── live guide (self-updating skill content) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS guide_section (
  name          TEXT PRIMARY KEY,
  body          TEXT NOT NULL,
  guide_version INTEGER NOT NULL DEFAULT 1,               -- monotonic per section
  updated_tx    INTEGER NOT NULL REFERENCES tx(tx_id)
);
CREATE TABLE IF NOT EXISTS guide_proposal (
  id         TEXT PRIMARY KEY,
  section    TEXT NOT NULL,
  body       TEXT NOT NULL,
  agent_id   TEXT NOT NULL,
  why        TEXT,
  status     TEXT NOT NULL DEFAULT 'proposed',            -- proposed | merged | rejected
  created_tx INTEGER NOT NULL REFERENCES tx(tx_id)
);

-- ── meta: schema_version counter + engine bookkeeping ───────────────────────────
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
