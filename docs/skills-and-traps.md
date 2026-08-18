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

## Making sure they are actually seen

Recording is useless if nobody reads it, so the surfacing is automatic rather than opt-in:

- **`graph_get` on a node returns its attached traps** (and project-wide traps scoped to the same
  subject version) — an agent reading a node cannot miss its dead-ends.
- **`graph_search` attaches matching traps** to the results with an explicit warning, so a query
  that matches a known dead-end says so before any time is spent.
- The server `instructions` and the bundled skill both tell agents to search the registries first
  and to publish/record as they go — a trap is recorded **when you abandon the approach**, not in
  a tidy-up at the end of the task.
