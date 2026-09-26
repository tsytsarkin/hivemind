---
name: hivemind-schema
description: >-
  Design the node and edge types for a Hivemind project — interview the user about their work,
  then propose and apply a schema. Use when a project has no schema yet, when the types the
  current work needs do not exist in the project you are writing to, or when the user asks to
  define or extend a project's vocabulary. Covers the two versioning axes and the generic edge
  traits that give a relationship its behaviour.
metadata:
  version: "1.5.0"
---

# Designing a Hivemind schema

> If you find yourself drafting type names before the user has answered a single question, stop.
> Inventing a vocabulary before you understand the work is the exact failure this skill exists to
> prevent, and it is the one mistake the engine will not let you take back.

A project's node and edge **types are its meaning** — the engine itself knows only mechanics. A
project with no types cannot be written to at all: every write is validated against a type, and an
unknown type is refused. A project with the *wrong* types is worse, because schema changes are
**additive-only and no tool removes a type**. A redundant type is therefore permanent, and it
splits the graph quietly: half the work lands under one name and half under its near-synonym, and
from then on every type-filtered search, every browse by type and every typed edge reaches only one
half of it.

**Near-duplicate type sprawl is the dominant long-term failure mode of a graph like this.** It
costs ten minutes to prevent here, and afterwards there is no remedy — nothing deletes a type, so
the best anyone can do later is stop using one and leave it in the schema forever. That is what
this skill is for.

## Do not propose a schema before you understand the work

Your **first message** to the user does two things: it offers the way out (next section), and it
asks these five questions. Ask all five at once, numbered, in their words — five short questions
in one turn get answered; five separate round trips get abandoned halfway. Follow up only where an
answer is too thin to model.

1. **What does this work track?** The nouns they already say out loud, in their language, not the
   engine's. Not the categories you think a system like this ought to have.
2. **Which of those things have versions of the thing itself?** A build, a release, a package
   version, a document revision that arrives from elsewhere. These belong on the **subject axis**:
   each version is its own cell, created with a stable `subject_key` plus the `subject_version`
   coordinate, and the cells coexist. Two subject cells that disagree are **not** in conflict —
   they describe different things, and reading one as a rebuttal of the other is how an agent
   later mistakes a version difference for a dispute.
3. **Which things instead get corrected over time?** Notes, conclusions, measurements that get
   revised as they learn more. Those ride the **revision axis**: you supersede the current head and
   the chain behind it stays walkable and queryable `as_of` an earlier moment. Every node gets this
   for free — it is never a type decision, and it must never be modelled as a relationship.
4. **What relationships matter, and where does each one come from?** A relationship a person
   curates by hand wants `versioned: true` — full per-edge history. A relationship imported in bulk
   from a tool (a call graph, a dependency graph, a reachability map) wants `versioned: false`,
   which routes it to the bulk table and is replaced wholesale under a `source_tag`. Millions of
   bulk edges are cheap; millions of versioned ones are not.
5. **What counts as a disagreement worth surfacing?** If two claims can conflict and somebody
   should notice before building on either, that relationship wants `assertive: true` — an open
   edge of that type then flags **both** endpoints as `disputed` on every read until somebody
   resolves it. The engine has no idea which of your relationships mean disagreement; the trait is
   the only way to get the behaviour.

Then read **`references/TRAITS.md`** in this skill directory and map the answers onto the traits.

## Offer the skip, plainly

Some people do not want to be interviewed, and a user trapped in an interview will invent answers,
which is worse than no interview. Say in one line, up front, that they can instead:

- start **bare** — you define each type with `schema_propose` at the moment the work demands it; or
- **inherit** the vocabulary of a project they already use.

Neither is a failure mode. A scratch or throwaway project is usually better off bare. If they skip,
skip — do not re-open the interview later in the same turn, and do not treat a one-word answer as
permission to design the whole vocabulary yourself.

**If there is nobody to ask** — a background job, an automated session, a user who has gone — that
is not permission to design the vocabulary alone. Start bare and add one type at a time as the work
forces each one into existence. A bare project stays correctable; an invented one does not.

Inheriting is a creation-time option (`project_create(..., schema="inherit")`) and it fires only
when the project is really created: called on a name that already exists, `project_create` returns
`existing: true` and copies nothing, and no tool deletes a project so it cannot be re-created. So
for a project that already exists and is empty, copy the types across yourself — `schema_get` on the
source, `schema_apply` here. Carry each edge type's **traits** over explicitly: `schema_get` reports
them as plain fields sitting next to the schema, and a pack that copies only the schema comes out
`versioned`, cyclic and unconstrained, because every trait you omit falls back to its default.

## Keep it small

**Five to eight node types to start.** Additive-only cuts both ways: a type you did not think of is
cheap to add the day you need it, while one you added speculatively is there forever. If you are
unsure whether two things are one type with a field or two types, **they are one type with a
field.**

Before proposing anything:

- `schema_get()` — if the project inherited or already holds types, extend them rather than
  duplicate. `graph_types()` shows which of the **node** types actually carry data; there is no
  edge-type census, so read the edge types out of `schema_get` itself.
- `guide_get()` — a deployment's guide sections carry its own naming conventions; follow them
  instead of inventing a parallel set.
- Read every proposed name against the existing ones for near-synonyms. `schema_propose` refuses a
  *new* name that looks like an existing type and tells you to reuse it, but that guard compares
  **spelling**: `run`/`runs` is caught, `note`/`observation` is not, and `schema_apply` does not run
  the check at all. Catching synonyms is your job and the user's, not the engine's.

## Propose, show, apply

1. **Draft the pack.** Node types with a JSON Schema each (`{"type": "object",
   "additionalProperties": true}` with a couple of named properties is a fine start — the graph
   exists to find things, not to validate them to death), and edge types with their traits stated
   explicitly rather than left to default. One of those types has to be able to hold plain prose —
   a decision, a note, a summary — or step 4 has nowhere to land and the project has no home for
   the reasoning behind the work either.
2. **Show the user the whole thing before applying it** — a compact table of type, what it holds,
   and why it exists. They will spot a wrong noun instantly. They will not spot a missing trait, so
   say in plain words what each trait you chose will do. This review is the only thing standing
   between a wrong draft and a permanent type, because `schema_apply` defines everything as active
   in one shot with no near-duplicate check.
3. **Apply it.** `schema_apply(pack)` for the whole pack at once — re-applying an unchanged pack is
   idempotent and does not inflate versions. `schema_propose(kind, name, json_schema, traits)` for
   one type at a time; a proposed type is usable for writes immediately, and `schema_promote` is the
   human step that makes it the active one.
4. **Record the rationale in the graph.** Write one node in the new project with `graph_upsert`,
   using the prose type from step 1 — the interview answers, the type list, why each type exists,
   and especially what you deliberately left out and why. A future agent reading `schema_get` can
   see *what* the vocabulary is; only this rationale node tells them *why*. Without it the next
   agent re-derives the vocabulary from scratch and adds exactly the duplicates you just avoided.
   Give it a stable identity — `subject_key="schema-rationale"` with `subject_version="-"`, since
   the two are only ever passed together — so the next agent can find it by name and supersede it
   rather than writing a second one nobody reaches.

## After the schema exists

Say in one line that the project is ready, that every Hivemind call from here on passes
`project=<name>`, and that write tools refuse when no project is resolvable — so the argument is
not optional.
