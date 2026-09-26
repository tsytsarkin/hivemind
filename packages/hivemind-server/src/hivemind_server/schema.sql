-- Hivemind core schema. SQLite. Domain-agnostic: node/edge *types* live in node_type/edge_type
-- as data; the engine hardcodes no relationship types.
--
-- SENTINEL for "current" (open) tx_to = 9223372036854775807 (max signed int64).

-- Agent collaboration metadata. Keep names distinct from the removed v1 bus_* tables:
-- db.py drops those old tables during startup on existing deployments.
CREATE TABLE IF NOT EXISTS chat_room (
  room_id       TEXT PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  description   TEXT NOT NULL,
  creator_user  TEXT NOT NULL,
  creator_device TEXT NOT NULL,
  creator_client TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_subscription (
  room_id       TEXT NOT NULL REFERENCES chat_room(room_id),
  user          TEXT NOT NULL,
  device        TEXT NOT NULL,
  client        TEXT NOT NULL,
  joined_at     TEXT NOT NULL,
  PRIMARY KEY (room_id, user, device, client)
);
CREATE INDEX IF NOT EXISTS ix_chat_subscription_address
  ON chat_subscription(user, device, client);

CREATE TABLE IF NOT EXISTS chat_message (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id     TEXT NOT NULL UNIQUE,
  channel        TEXT NOT NULL CHECK (channel IN ('dm', 'room')),
  room_id        TEXT REFERENCES chat_room(room_id),
  target_user    TEXT,
  target_device  TEXT,
  target_client  TEXT,
  sender_user    TEXT NOT NULL,
  sender_device  TEXT NOT NULL,
  sender_client  TEXT NOT NULL,
  sender_session TEXT,
  target_key     TEXT NOT NULL,
  body           TEXT NOT NULL,
  body_bytes     INTEGER NOT NULL,
  message_kind   TEXT NOT NULL CHECK (message_kind IN ('text', 'progress')),
  retry_key      TEXT NOT NULL,
  created_at     REAL NOT NULL,
  UNIQUE (sender_user, sender_device, sender_client, target_key, retry_key)
);
CREATE INDEX IF NOT EXISTS ix_chat_message_target
  ON chat_message(channel, target_user, target_device, target_client, seq);
CREATE INDEX IF NOT EXISTS ix_chat_message_room
  ON chat_message(room_id, seq);
CREATE INDEX IF NOT EXISTS ix_chat_message_expiry ON chat_message(created_at);

CREATE TABLE IF NOT EXISTS chat_cursor (
  user           TEXT NOT NULL,
  device         TEXT NOT NULL,
  client         TEXT NOT NULL,
  target_key     TEXT NOT NULL,
  seq            INTEGER NOT NULL,
  updated_at     REAL NOT NULL,
  PRIMARY KEY (user, device, client, target_key)
);
CREATE TABLE IF NOT EXISTS chat_expiration_watermark (
  target_key          TEXT PRIMARY KEY,
  expired_through_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_session (
  user             TEXT NOT NULL,
  device           TEXT NOT NULL,
  client           TEXT NOT NULL,
  session_id       TEXT NOT NULL,
  last_activity_at REAL NOT NULL,
  PRIMARY KEY (user, device, client, session_id)
);
CREATE INDEX IF NOT EXISTS ix_chat_session_last_activity ON chat_session(last_activity_at);
CREATE TABLE IF NOT EXISTS chat_usage (
  id            INTEGER PRIMARY KEY CHECK (id=1),
  counted_bytes INTEGER NOT NULL,
  message_count INTEGER NOT NULL
);

-- ── provenance ────────────────────────────────────────────────────────────────
-- One row per write. tx_id is the monotonic "as-of" coordinate for the revision axis.
CREATE TABLE IF NOT EXISTS tx (
  tx_id    INTEGER PRIMARY KEY,
  tx_time  TEXT    NOT NULL,                              -- ISO-8601 UTC
  agent_id TEXT    NOT NULL,                              -- free-form LABEL ("which job was this")
  user_id  TEXT,                                          -- the token's user: the real author
  device   TEXT,                                          -- the token's device, for the same reason
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
  created_by      TEXT,                                  -- user who first created it (vs. last author)
  created_tx      INTEGER NOT NULL REFERENCES tx(tx_id)
);
-- one node per (subject_key, subject_version) cell → upsert-by-subject is deterministic
CREATE UNIQUE INDEX IF NOT EXISTS ux_subject
  ON node(subject_key, subject_version) WHERE subject_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_subject_order ON node(subject_key, subject_order);
CREATE INDEX IF NOT EXISTS ix_node_type ON node(node_type);
CREATE INDEX IF NOT EXISTS ix_node_created_by ON node(created_by);

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
  author_user  TEXT,                                     -- token's user; NULL = written before authorship
  tx_from      INTEGER NOT NULL REFERENCES tx(tx_id),
  tx_to        INTEGER NOT NULL DEFAULT 9223372036854775807,
  retracted    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (node_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_node_head
  ON node_version(node_id) WHERE tx_to = 9223372036854775807;
CREATE INDEX IF NOT EXISTS ix_node_asof ON node_version(node_id, tx_from, tx_to);
CREATE INDEX IF NOT EXISTS ix_node_ver_hash ON node_version(content_hash);
-- The contributor chain is COMPUTED from these rows (GROUP BY author_user), never stored as an
-- array on the node: a denormalized author list is a second copy of the truth and drifts from it.
CREATE INDEX IF NOT EXISTS ix_node_ver_author ON node_version(author_user);

CREATE TABLE IF NOT EXISTS edge_version (
  version_id   TEXT PRIMARY KEY,
  edge_id      TEXT NOT NULL REFERENCES edge(edge_id),
  seq          INTEGER NOT NULL,
  prev_version TEXT REFERENCES edge_version(version_id),
  props        TEXT NOT NULL CHECK (json_valid(props)),
  schema_ver   INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  author_user  TEXT,                                     -- token's user; NULL = written before authorship
  tx_from      INTEGER NOT NULL REFERENCES tx(tx_id),
  tx_to        INTEGER NOT NULL DEFAULT 9223372036854775807,
  retracted    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (edge_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_edge_head
  ON edge_version(edge_id) WHERE tx_to = 9223372036854775807;
CREATE INDEX IF NOT EXISTS ix_edge_asof ON edge_version(edge_id, tx_from, tx_to);
CREATE INDEX IF NOT EXISTS ix_edge_ver_author ON edge_version(author_user);

-- ── bulk edges (versioned=0): high-volume imported graphs, no per-edge history ───
CREATE TABLE IF NOT EXISTS edge_bulk (
  edge_type   TEXT NOT NULL,
  src_node_id TEXT NOT NULL,
  dst_node_id TEXT NOT NULL,
  props       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(props)),
  source_tag  TEXT NOT NULL,                             -- e.g. "kernelcache@26A5388g"; replace-by-tag
  -- No author_user here, deliberately: a bulk edge has no version row to carry one. Bulk edges are
  -- attributed through their created_tx (which records user_id) and their source_tag alone, so
  -- "every edge carries an author" is true of versioned edges only.
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
  author_user     TEXT,                                   -- the token's user who published it
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
  id          TEXT PRIMARY KEY,
  section     TEXT NOT NULL,
  body        TEXT NOT NULL,
  agent_id    TEXT NOT NULL,                              -- free-form agent LABEL
  author_user TEXT,                                       -- the token's user who proposed it
  why         TEXT,
  status      TEXT NOT NULL DEFAULT 'proposed',           -- proposed | merged | rejected
  created_tx  INTEGER NOT NULL REFERENCES tx(tx_id)
);

-- ── meta: schema_version counter + engine bookkeeping ───────────────────────────
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- ── full-text search (app-maintained; props are arbitrary JSON) ─────────────────
-- Two tokenizers: unicode61 for prose, trigram for symbols/paths (survives Foo::bar_baz).
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(node_id UNINDEXED, body, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS sym_fts  USING fts5(node_id UNINDEXED, body, tokenize='trigram');

-- ── mini-skills: documented procedures (CoALA "procedural memory") ───────────────
-- Same immutable-version flow as the tool registry: a published version is never edited,
-- supersede by publishing a new semver; retire with a yank.
CREATE TABLE IF NOT EXISTS skill (
  id             TEXT PRIMARY KEY,                       -- e.g. "re/unpack-dyld-cache"
  latest_version TEXT,
  created_tx     INTEGER NOT NULL REFERENCES tx(tx_id)
);
CREATE TABLE IF NOT EXISTS skill_version (
  id            TEXT NOT NULL REFERENCES skill(id),
  version       TEXT NOT NULL,                           -- exact semver, immutable
  title         TEXT NOT NULL,
  description   TEXT NOT NULL,                           -- what it does AND when to use it
  when_to_use   TEXT,                                    -- trigger phrases
  body          TEXT NOT NULL,                           -- the procedure (markdown)
  tags          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
  requires      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(requires)),  -- tools/skills needed
  verified_how  TEXT,                                    -- how the author confirmed it works
  author        TEXT,                                    -- free-form agent LABEL
  author_user   TEXT,                                    -- the token's user: the real author
  yanked        INTEGER NOT NULL DEFAULT 0,
  yanked_reason TEXT,
  created_tx    INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (id, version)
);
CREATE VIRTUAL TABLE IF NOT EXISTS skill_fts USING fts5(id UNINDEXED, body, tokenize='unicode61');

-- ── traps: recorded dead-ends / wrong approaches (CoALA "episodic memory") ────────
-- Evidence-bearing by construction: a trap without what-was-tried and what-happened is not a
-- trap, it is an opinion. Falsifiable (status) and scopeable (node and/or subject-version), so a
-- stale or wrong trap can be retired or disputed instead of silently misleading everyone.
CREATE TABLE IF NOT EXISTS trap (
  trap_id      TEXT PRIMARY KEY,
  title        TEXT NOT NULL,
  what_failed  TEXT NOT NULL,                            -- the approach that was tried
  symptom      TEXT NOT NULL,                            -- what was actually observed
  root_cause   TEXT,                                     -- why, once understood
  instead      TEXT,                                     -- what to do in its place
  node_id      TEXT REFERENCES node(node_id),            -- NULL = project-wide
  subject_key  TEXT, subject_version TEXT,               -- optional: only true for this version
  cost_minutes INTEGER,                                  -- how much time it burned
  evidence     TEXT,                                     -- log/digest/command that shows it
  verified_how TEXT,                                     -- measured | reproduced | inferred
  confidence   TEXT NOT NULL DEFAULT 'medium',           -- low | medium | high
  status       TEXT NOT NULL DEFAULT 'active',           -- active | retired | disputed
  status_reason TEXT,
  author       TEXT,                                     -- free-form agent LABEL
  author_user  TEXT,                                     -- the token's user: the real author
  created_tx   INTEGER NOT NULL REFERENCES tx(tx_id),
  updated_tx   INTEGER NOT NULL REFERENCES tx(tx_id)
);
CREATE INDEX IF NOT EXISTS ix_trap_node ON trap(node_id) WHERE node_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_trap_subject ON trap(subject_key, subject_version);
CREATE INDEX IF NOT EXISTS ix_trap_status ON trap(status);
CREATE VIRTUAL TABLE IF NOT EXISTS trap_fts USING fts5(trap_id UNINDEXED, body, tokenize='unicode61');

-- ── skill ↔ graph links: which nodes a procedure is about ────────────────────────
-- Gives topical organisation today (find skills from the thing you are looking at) and is the
-- seed data for a real skill graph later, if the library ever grows big enough to need one.
CREATE TABLE IF NOT EXISTS skill_link (
  id         TEXT NOT NULL REFERENCES skill(id),
  node_id    TEXT NOT NULL REFERENCES node(node_id),
  relation   TEXT NOT NULL DEFAULT 'about',        -- about | uses | produces | supersedes_manual
  source     TEXT NOT NULL DEFAULT 'auto',         -- auto | confirmed  (confirmed wins, never downgraded)
  score      REAL,                                 -- retrieval score when auto-linked
  note       TEXT,
  created_tx INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (id, node_id, relation)
);
CREATE INDEX IF NOT EXISTS ix_skill_link_node ON skill_link(node_id);

-- ── tool discovery: FTS + graph links (mirrors the mini-skill library) ───────────
CREATE VIRTUAL TABLE IF NOT EXISTS tool_fts USING fts5(id UNINDEXED, body, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS tool_link (
  id         TEXT NOT NULL REFERENCES tool(id),
  node_id    TEXT NOT NULL REFERENCES node(node_id),
  relation   TEXT NOT NULL DEFAULT 'about',        -- about | analyses | produces
  source     TEXT NOT NULL DEFAULT 'auto',
  score      REAL,
  note       TEXT,
  created_tx INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (id, node_id, relation)
);
CREATE INDEX IF NOT EXISTS ix_tool_link_node ON tool_link(node_id);

-- ── embeddings for semantic search over skills and tools ─────────────────────────
CREATE TABLE IF NOT EXISTS embedding (
  kind       TEXT NOT NULL,                        -- 'skill' | 'tool'
  item_id    TEXT NOT NULL,
  model      TEXT NOT NULL,                        -- which backend produced it
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,                        -- float32 little-endian, L2-normalised
  updated_tx INTEGER NOT NULL REFERENCES tx(tx_id),
  PRIMARY KEY (kind, item_id)
);
