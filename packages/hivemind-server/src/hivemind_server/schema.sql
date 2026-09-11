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
  author        TEXT,
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
  author       TEXT,
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

-- ── agent bus: ephemeral coordination that is NOT knowledge ──────────────────────
-- Deliberately outside the graph. The graph is versioned truth (supersession chains, FTS,
-- backup, GC); coordination traffic is ephemeral, high-volume, totally ordered and read once.
-- Nothing here references tx(tx_id): bus rows are written via Database.write_light() and are
-- reaped on a TTL, so a provenance row per chat message would outlive its own message.
--
-- Identity is a per-SESSION id minted by the server, never the bearer token: one token is
-- reused across many agents on many harnesses whose capabilities differ (see docs/bus.md).
CREATE TABLE IF NOT EXISTS bus_session (
  session_id    TEXT PRIMARY KEY,                       -- server-minted ULID
  label         TEXT NOT NULL,                          -- human-readable, e.g. "opus5@studio"
  harness       TEXT,                                   -- claude-code | codex | cli | ...
  interruptible INTEGER NOT NULL DEFAULT 0,             -- can a sidecar actually wake it?
  client_id     TEXT,                                   -- authenticated token id: attribution only
  meta          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta)),
  cursor        INTEGER NOT NULL DEFAULT 0,             -- highest seq this session has drained
  started_at    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  expires_at    TEXT NOT NULL,                          -- heartbeat deadline
  ttl           INTEGER NOT NULL DEFAULT 900,           -- seconds a heartbeat OR a poll extends by
  ended_at      TEXT                                    -- set by bus_bye; NULL = live
);
CREATE INDEX IF NOT EXISTS ix_bus_session_live ON bus_session(expires_at) WHERE ended_at IS NULL;

-- Specific, dotted capability names (device.iphone.attached, browser.cdp) so prefix queries work.
CREATE TABLE IF NOT EXISTS bus_capability (
  session_id TEXT NOT NULL REFERENCES bus_session(session_id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  attrs      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(attrs)),
  PRIMARY KEY (session_id, name)
);
CREATE INDEX IF NOT EXISTS ix_bus_cap_name ON bus_capability(name);

CREATE TABLE IF NOT EXISTS bus_membership (
  session_id TEXT NOT NULL REFERENCES bus_session(session_id) ON DELETE CASCADE,
  room       TEXT NOT NULL,
  joined_at  TEXT NOT NULL,
  PRIMARY KEY (session_id, room)
);
CREATE INDEX IF NOT EXISTS ix_bus_member_room ON bus_membership(room);

-- One global AUTOINCREMENT seq: a single integer cursor covers every room, and total order
-- across rooms is free. AUTOINCREMENT (not plain rowid) so a reaped tail cannot cause seq reuse,
-- which would silently rewind every cursor pointing past it.
CREATE TABLE IF NOT EXISTS bus_message (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  room       TEXT NOT NULL DEFAULT 'lobby',
  sender     TEXT NOT NULL,                             -- session_id, or 'system'
  to_session TEXT,                                      -- NULL = room broadcast
  kind       TEXT NOT NULL DEFAULT 'chat',              -- chat|question|request|response|claim|system
  body       TEXT NOT NULL,
  data       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data)),
  request_id TEXT,                                      -- correlation for request/response
  -- Threading, so an answer is attached to its question instead of merely adjacent to it in a
  -- busy room. SET NULL rather than CASCADE on purpose: TTL is per-message and a question
  -- expires BEFORE the answers it provoked, so cascading would delete the answers with it.
  reply_to   INTEGER REFERENCES bus_message(seq) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bus_msg_room ON bus_message(room, seq);
CREATE INDEX IF NOT EXISTS ix_bus_msg_to ON bus_message(to_session, seq);
CREATE INDEX IF NOT EXISTS ix_bus_msg_expiry ON bus_message(expires_at);
CREATE INDEX IF NOT EXISTS ix_bus_msg_reply ON bus_message(reply_to) WHERE reply_to IS NOT NULL;

-- Open-claim work distribution: the request is offered to every live session matching `needs`,
-- and exactly one wins via UPDATE ... WHERE claimed_by IS NULL. A dead advertiser simply never
-- claims; a claimant that dies is reaped when its lease expires and the request reopens.
CREATE TABLE IF NOT EXISTS bus_request (
  request_id       TEXT PRIMARY KEY,
  requester        TEXT NOT NULL,                       -- session_id
  needs            TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(needs)),  -- capability patterns
  to_session       TEXT,                                -- direct address; skips the claim race
  task             TEXT NOT NULL,
  payload          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
  room             TEXT,
  state            TEXT NOT NULL DEFAULT 'open',        -- open|claimed|done|failed|expired|cancelled
  claimed_by       TEXT,
  claimed_at       TEXT,
  lease_expires_at TEXT,
  attempts         INTEGER NOT NULL DEFAULT 0,          -- claims that were reaped, for visibility
  result           TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bus_req_state ON bus_request(state, expires_at);
CREATE INDEX IF NOT EXISTS ix_bus_req_lease ON bus_request(state, lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_bus_req_claim ON bus_request(claimed_by, state);

-- What a message or request POINTS AT in the graph. The bus stays domain-agnostic and holds no
-- knowledge itself, but "do this to that thing" is useless without the that: a worker needs the
-- subject of the work, not a prose description of it.
--
-- `spec` is the typed reference (node | version | subject | traversal | search); `anchor` is the
-- node_id denormalised out of it purely so the reverse lookup ("what live traffic points at this
-- node?") is an index hit. Deliberately one-way: the bus points into the graph and the graph
-- never points back, so ephemeral traffic can be reaped without leaving the graph dangling.
CREATE TABLE IF NOT EXISTS bus_ref (
  ref_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  message_seq INTEGER REFERENCES bus_message(seq) ON DELETE CASCADE,
  request_id  TEXT    REFERENCES bus_request(request_id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,                        -- node|version|subject|traversal|search
  anchor      TEXT,                                 -- node_id, when the ref has one
  spec        TEXT NOT NULL CHECK (json_valid(spec)),
  role        TEXT NOT NULL DEFAULT 'context',      -- context|target|evidence|result
  note        TEXT,
  created_at  TEXT NOT NULL,
  CHECK (message_seq IS NOT NULL OR request_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_bus_ref_anchor ON bus_ref(anchor) WHERE anchor IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_bus_ref_msg ON bus_ref(message_seq);
CREATE INDEX IF NOT EXISTS ix_bus_ref_req ON bus_ref(request_id);

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
