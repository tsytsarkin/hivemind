# Mini-skills and traps

Hivemind stores three kinds of memory for an agent fleet. Borrowing the
[CoALA](https://arxiv.org/abs/2309.02427) taxonomy:

| Memory | "What is true" | "How to do it" | "What happened when I tried" |
|---|---|---|---|
| CoALA term | semantic | procedural | episodic |
| In Hivemind | the **graph** (nodes/edges) | **tools** (executable) + **mini-skills** (documented) | **traps** |

## Mini-skills — procedures worth keeping

A mini-skill is a procedure an agent worked out: the sequence, the gotchas, the thing that only
works if you do it in the right order. It is prose, not code — for executable artifacts use the
tool registry.

**Versioning is identical to tools on purpose:** a published version is **immutable**, you revise
by publishing a new semver, and you retire with a **yank** (never a delete, so an exact pin keeps
resolving). Only the newest non-yanked version is indexed for search.

```sh
skill_search("unpack a shared cache")        # ALWAYS look before deriving from scratch
skill_get("re/unpack-dyld-cache")            # full procedure
skill_publish(id, version, title, description, body, verified_how="ran it on two builds")
skill_yank(id, version, reason="superseded by the ipsw flow")
```
Fields worth care: `description` says *what and when* (it is what search shows), `when_to_use`
carries trigger phrases, `requires` names tools/skills it depends on, and **`verified_how`** records
how the author confirmed it works. Bodies are capped (~5k tokens) — a mini-skill, not a manual.

*Why `verified_how`:* [Voyager](https://arxiv.org/abs/2305.16291), the canonical agent skill
library, only admits a skill to the library after self-verification. An unverified procedure is a
guess, and a guess published as a skill costs the next agent more than it saves.


### Finding what exists

Three surfaces, cheapest first:

```sh
skill_catalog()                      # browse: topics with counts + one line per skill
skill_catalog(topic="ops")           # narrow to a tag
skill_search("restart the server")   # full-text over id/title/description/when_to_use/body
skill_get("ops/restart-hivemind")    # the procedure itself
```
Also over plain HTTP, for dashboards or non-MCP clients:
`GET /p/<project>/skills[?topic=…]` and `GET /p/<project>/skills/<id>[?constraint=^1.0]`.

**Skills are also discoverable from the graph.** `skill_link(skill_id, node_id, relation)`
attaches a procedure to the thing it is about, and `graph_get` on that node then returns its
skills alongside its traps — so an agent looking at a component finds the procedures for it
without having to guess a search query.

### Avoiding duplicates

Publishing a **new id** that closely resembles an existing skill (by name or description) is
**refused**, and the error names the skill to revise instead:

> a similar skill already exists: ops/restart-server@1.0.0 (similar name). Publish a NEW VERSION
> of it instead of a duplicate, or re-publish with force=true if this is genuinely different.

Bumping the version of an *existing* skill is never blocked — that is the intended way to revise.
`force=true` overrides and records a warning on the publish result. The check is deterministic
string similarity, not embeddings: enough to stop the same procedure being written twice under two
names, which is the real failure mode at this library size.

### Field reference

| Field | Required | Notes |
|---|---|---|
| `id` | ✔ | stable identifier, lowercase `[a-z0-9-_./]` — namespace it (`ops/restart-server`) |
| `version` | ✔ | exact semver; ranges (`^1.0`) are rejected. Immutable once published |
| `title` | ✔ | one line |
| `description` | ✔ | **what it does AND when to use it** — this is what `skill_search` shows |
| `body` | ✔ | the procedure, markdown, ≤ 20 000 chars (~5k tokens) |
| `when_to_use` | | trigger phrases, to help retrieval |
| `tags` | | list of strings; `skill_search(tags=[…])` filters on them |
| `requires` | | free-form object naming tools/skills this depends on |
| `verified_how` | | how you confirmed it actually works — say "untested" rather than nothing |
| `author` | auto | the calling `agent` |
| `yanked`, `yanked_reason` | auto | set by `skill_yank` |

### Writing one worth reading

- **Steps another agent can execute**, not a narrative of how you got there.
- **Put the gotcha in.** The reason this beats re-deriving is usually one non-obvious line.
- **Small.** Link to a tool, a node, or a guide section rather than inlining detail.
- **Say how you verified it.** A procedure nobody has run is a hypothesis.

### From the shell

```sh
hivemind skill search "restart the server"
hivemind skill get ops/restart-hivemind [--constraint '^1.0']
hivemind skill publish ops/restart-hivemind --version 1.1.0 \
    --title "Restart the server" --description "…what and when…" \
    --body-file ./procedure.md --tag ops --verified-how "ran it on the lab box"
hivemind skill yank ops/restart-hivemind 1.0.0 --reason "superseded"
```

## Traps — dead-ends worth remembering

A trap records an approach that looked reasonable and wasn't: what was tried, what actually
happened, and what to do instead.

```sh
trap_search("regex parse")                   # check BEFORE starting an approach
trap_record(title, what_failed, symptom, root_cause=…, instead=…, cost_minutes=90,
            node_id=…, subject_key=…, subject_version=…)
trap_status(trap_id, "disputed"|"retired", reason)
```

Three properties are deliberate, and they exist because of a documented failure mode:
[Reflexion](https://arxiv.org/abs/2303.11366)-style agents that store free-form self-reflections
suffer **memory confabulation** — confident but incorrect reflections get written down, reused, and
become self-reinforcing false beliefs.

1. **Evidence is required by shape.** `what_failed` (what you actually tried) and `symptom` (what
   you actually observed) are mandatory. A trap with neither is an opinion the next agent cannot
   evaluate, so the API rejects it.
2. **Traps are scoped.** Attach to a node (`node_id`) and/or a version
   (`subject_key`+`subject_version`). "True on build A" must never silently become "true
   everywhere" — a trap scoped to `X@26.6` does not surface on `X@27.0`.
3. **Traps are falsifiable.** Anyone can mark one `disputed` (with evidence it's wrong) or
   `retired` (no longer applies), always with a reason. **Disputed traps stay visible**; only
   retired ones drop out of search. A misleading trap is worse than no trap.

Treat a trap as a *prior recorded by an agent who may have been wrong*, never as proof.


### Finding what exists

Three surfaces, cheapest first:

```sh
skill_catalog()                      # browse: topics with counts + one line per skill
skill_catalog(topic="ops")           # narrow to a tag
skill_search("restart the server")   # full-text over id/title/description/when_to_use/body
skill_get("ops/restart-hivemind")    # the procedure itself
```
Also over plain HTTP, for dashboards or non-MCP clients:
`GET /p/<project>/skills[?topic=…]` and `GET /p/<project>/skills/<id>[?constraint=^1.0]`.

**Skills are also discoverable from the graph.** `skill_link(skill_id, node_id, relation)`
attaches a procedure to the thing it is about, and `graph_get` on that node then returns its
skills alongside its traps — so an agent looking at a component finds the procedures for it
without having to guess a search query.

### Avoiding duplicates

Publishing a **new id** that closely resembles an existing skill (by name or description) is
**refused**, and the error names the skill to revise instead:

> a similar skill already exists: ops/restart-server@1.0.0 (similar name). Publish a NEW VERSION
> of it instead of a duplicate, or re-publish with force=true if this is genuinely different.

Bumping the version of an *existing* skill is never blocked — that is the intended way to revise.
`force=true` overrides and records a warning on the publish result. The check is deterministic
string similarity, not embeddings: enough to stop the same procedure being written twice under two
names, which is the real failure mode at this library size.

### Field reference

| Field | Required | Notes |
|---|---|---|
| `title` | ✔ | one line, the dead-end in a sentence |
| `what_failed` | ✔ | **what you actually tried** — the command, flag, approach |
| `symptom` | ✔ | **what you actually observed** — the error, hang, silent wrong result |
| `root_cause` | | why, once you understood it |
| `instead` | | what to do in its place (the most valuable field for the next agent) |
| `node_id` | | attach to a node; omit for project-wide |
| `subject_key`, `subject_version` | | scope to one version of a thing |
| `cost_minutes` | | how much time it burned — makes the cost of not reading it visible |
| `evidence` | | a log line, digest, or command that shows it |
| `verified_how` | | `measured` \| `reproduced` \| `inferred` |
| `confidence` | | `low` \| `medium` \| `high` (default `medium`) |
| `status` | auto | `active` \| `disputed` \| `retired`, set via `trap_status` |

### Scoping decides who sees it

| How you record it | Surfaces on |
|---|---|
| `node_id=…` | `graph_get` of that node; `trap_search` |
| `subject_key` + `subject_version` | `graph_get` of nodes in that subject cell; `trap_search` |
| `subject_key` only | `graph_get` of **any** version of that subject; `trap_search` |
| neither (project-wide) | `trap_search` and `graph_search` matches — **not** on `graph_get` |

Scope as narrowly as the evidence supports. An unscoped trap is a claim about the whole project.

### From the shell

```sh
hivemind trap search "regex parse" [--node-id …] [--include-retired]
hivemind trap get <trap_id>
hivemind trap record --title "…" --what-failed "…" --symptom "…" \
    --root-cause "…" --instead "…" --cost-minutes 90 --confidence high \
    [--node-id … | --subject-key X --subject-version 26.6]
hivemind trap status <trap_id> retired --reason "fixed upstream"
```

### A worked example

```
title:        pkill -f hivemind_server silently misses the process
what_failed:  used pkill -f 'hivemind_server' (underscore) before restarting
symptom:      old process kept running and served stale code; restart appeared to succeed
root_cause:   the process name uses a hyphen (hivemind-server), so the pattern never matched
instead:      fuser -k 8787/tcp, then confirm the PID actually changed
cost_minutes: 20        verified_how: reproduced        confidence: high
```
Note what makes it useful: it is falsifiable (you can re-run it), scoped (project-wide because the
process name is), and `instead` means the next agent spends zero minutes on it.

## Making sure they are actually seen

Recording is useless if nobody reads it, so the surfacing is automatic rather than opt-in:

- **`graph_get` on a node returns its attached traps** (and project-wide traps scoped to the same
  subject version) — an agent reading a node cannot miss its dead-ends.
- **`graph_search` attaches matching traps** to the results with an explicit warning, so a query
  that matches a known dead-end says so before any time is spent.
- The server `instructions` and the bundled skill both tell agents to search the registries first
  and to publish/record as they go — a trap is recorded **when you abandon the approach**, not in
  a tidy-up at the end of the task.

## Why not a skill graph (yet)

The obvious next step is to make skills nodes in a graph — topic edges, prerequisite edges,
retrieval by diffusion. [Graph-of-Skills](https://arxiv.org/html/2604.05333) builds exactly that
(dependency, workflow, semantic and alternative edges; hybrid seeds then Personalized PageRank),
and its own numbers say when it is worth it: **at 200 skills flat retrieval still slightly wins
(32.5 vs 32.1 reward); the graph only pulls ahead once the library is "moderately large"**, with
gains shown up to 2,000 skills. This library is nowhere near that, so the graph would be
machinery without a problem.

What that paper *does* justify now:
- its semantic edges exist partly to link **near-duplicate** skills — duplication is a real, known
  failure of flat libraries, so it is guarded at publish time instead;
- its dependency edges are derived **deterministically from I/O compatibility, with no LLM** —
  which means the graph can be *computed later* from data recorded now.

So the current design records the graph's raw material without paying for the graph: `requires`
captures prerequisites, `tags` capture topics, and `skill_link` captures what a skill is about.
If the library ever grows past a few hundred skills, those three fields are enough to build
dependency/semantic edges and switch retrieval over — without re-modelling anything.

Related reading: [SkillFlow](https://arxiv.org/html/2504.06188v2) (multi-stage narrowing: dense →
rerank → LLM select) and the [ecosystem-scale survey](https://arxiv.org/html/2605.07358v1), which
both find that **too many irrelevant skills degrades agent performance** — an argument for better
discovery, not a bigger library.

## Lifecycle and precedence

- **Skills** accumulate versions; search shows only the newest non-yanked one, `skill_get(id,
  constraint)` resolves like the tool registry (`^`, `~`, `>=`, exact). A yanked version stays
  fetchable by exact pin so anything that pinned it keeps working.
- **Traps** are mutable records with provenance: every `trap_record` / `trap_status` writes a `tx`
  row (who, when, why), so the history of a claim is auditable even though the row is updated in
  place.
- **Retire vs dispute:** `retired` = no longer applies (fixed, or it was version-specific and you
  have moved on) — drops out of search. `disputed` = you have evidence it is wrong — **stays
  visible**, because silently hiding a contested claim is how a graph loses the disagreement.

## Storage

Both live in the engine (not a pack) because they describe the agent workflow, not any domain:
`skill` / `skill_version` (immutable rows, mirroring `tool` / `tool_version`) and `trap`
(one row, updated in place, with `tx` provenance), each with its own FTS5 index. See
[data-model.md](data-model.md).
