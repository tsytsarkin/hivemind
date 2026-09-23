"""The skill is a shipped artifact, so its contract is checked the way any other artifact is.

Every assertion here guards content that has to survive a later edit: the interview, both
versioning axes, the trait table, the skip path, the size bound and the rationale node.

The checks are deliberately pinned to STRUCTURE rather than to a bare substring. `"assertive" in
body` passes on prose that names the trait once and teaches nothing about it; `"before" in body`
cannot fail at all. So a trait has to appear as a table row that says when to use it, the
interview has to be a numbered list of questions, and the "ask first" rule has to be a heading.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SKILL = ROOT / "plugin" / "skills" / "hivemind-schema" / "SKILL.md"
TRAITS = ROOT / "plugin" / "skills" / "hivemind-schema" / "references" / "TRAITS.md"
COMMAND = ROOT / "plugin" / "commands" / "project.md"

# Every generic edge trait the engine resolves (schemas.edge_traits / edge_type in schema.sql).
# A schema author who does not know one of these will model it as a node type instead.
ALL_TRAITS = ("versioned", "symmetric", "transitive", "acyclic", "assertive", "directed",
              "src_types", "dst_types", "cardinality")

# Traits the engine STORES and reports but that no engine code reads (verified against graph.py:
# only symmetric, acyclic, versioned, assertive and src/dst_types change what a write or read
# does). Saying otherwise teaches a mechanism that does not exist, which is worse than omitting it.
UNENFORCED = ("transitive", "cardinality", "directed")


def _frontmatter(text: str) -> str:
    return text.split("---")[1]


def _description(text: str) -> str:
    """The `description:` value, including its folded continuation lines."""
    out, collecting = [], False
    for line in _frontmatter(text).splitlines():
        if line.startswith("description:"):
            collecting = True
            out.append(line.split(":", 1)[1].strip().lstrip(">|-").strip())
        elif collecting:
            if line.startswith((" ", "\t")):
                out.append(line.strip())
            else:
                break
    return " ".join(p for p in out if p)


def _table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # split on unescaped pipes only: a cell may legitimately contain `\|` (e.g. "1:1 \| N:N")
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if all(set(c) <= set("-: ") for c in cells):       # separator row
            continue
        rows.append(cells)
    return rows


def _paragraphs(text: str) -> list[str]:
    """Body paragraphs with hard wrapping undone, so a sentence can be matched across lines."""
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text)]


def test_the_skill_ships():
    assert SKILL.is_file() and TRAITS.is_file()


def test_frontmatter_has_only_portable_fields():
    head = _frontmatter(SKILL.read_text())
    keys = {line.split(":")[0].strip() for line in head.splitlines() if ":" in line
            and not line.startswith((" ", "\t"))}
    assert "name" in keys and "description" in keys
    assert keys <= {"name", "description", "allowed-tools", "metadata", "license", "version"}
    assert re.search(r"^name:\s*hivemind-schema\s*$", head, re.M), "the directory name is the id"


def test_the_description_triggers_on_project_creation_and_missing_types():
    """project_create(schema='interview') is the only caller that names this skill, so the
    description is what has to catch the other two cases on its own."""
    desc = _description(SKILL.read_text())
    low = desc.lower()
    for cue in ("schema", "project", "node", "edge"):
        assert cue in low, f"description should mention {cue} so the skill is discoverable"
    assert "use when" in low, "a description without a trigger clause is not discoverable"
    # the three situations: a project with no types, work whose types are missing, an extension
    for situation in ("no schema", "do not exist", "extend"):
        assert situation in low, f"description should name the '{situation}' case"


@pytest.mark.parametrize("trait", ALL_TRAITS)
def test_it_names_every_generic_edge_trait(trait):
    """Named in a table row that also says when to reach for it — not merely mentioned."""
    rows = [r for r in _table_rows(TRAITS.read_text()) if trait in r[0]]
    assert rows, f"missing trait row: {trait}"
    assert any(len(r) >= 3 and len(r[2]) > 10 for r in rows), \
        f"trait {trait} has no 'use it when' guidance"


@pytest.mark.parametrize("trait", UNENFORCED)
def test_the_traits_the_engine_does_not_act_on_say_so(trait):
    """These are stored in edge_type and returned by schema_get, but no engine code reads them.
    A table implying they change behaviour is a false mechanism."""
    rows = [r for r in _table_rows(TRAITS.read_text()) if trait in r[0]]
    assert any("not enforced" in " ".join(r).lower() for r in rows), \
        f"{trait} is declarative only; the table must say so"


def test_the_symmetric_trait_explains_what_it_actually_does():
    """upsert_edge canonicalises orientation for a symmetric type; traversal does NOT mirror,
    so an author who reads 'A->B implies B->A' will write a query that returns nothing."""
    body = TRAITS.read_text()
    assert 'direction="both"' in body or "direction='both'" in body


def test_it_teaches_both_versioning_axes():
    body = SKILL.read_text().lower()
    assert "subject_key" in body and "subject_version" in body
    assert "revision" in body and "supersede" in body


def test_it_says_two_subject_cells_that_disagree_are_not_in_conflict():
    """The trap between the axes: mistaking a version difference for a disagreement."""
    hits = [p for p in _paragraphs(SKILL.read_text() + "\n\n" + TRAITS.read_text())
            if "subject" in p.lower() and "conflict" in p.lower()]
    assert hits, "neither file connects subject cells to what is NOT a conflict"
    assert any(re.search(r"\bnot\b", p, re.I) for p in hits)


def test_it_requires_asking_before_proposing():
    body = SKILL.read_text()
    headings = [h.lower() for h in body.splitlines() if h.startswith("#")]
    assert any("before" in h and ("propose" in h or "schema" in h or "vocabulary" in h)
               for h in headings), "the ask-first rule must be a heading, not a buried sentence"
    questions = re.findall(r"^\d+\.\s+\*\*", body, re.M)
    assert len(questions) >= 5, f"the interview is {len(questions)} questions, want 5+"


def test_it_offers_the_skip_path():
    """A user who does not want to be interviewed must not be trapped in one."""
    body = SKILL.read_text().lower()
    assert "bare" in body and ("skip" in body or "rather not" in body)
    assert "inherit" in body, "the other way out of the interview is inheriting a vocabulary"


def test_it_warns_against_type_sprawl_with_a_concrete_bound():
    body = SKILL.read_text()
    assert re.search(r"\b(five to eight|5[-–– ]to[-– ]8|5\s*[-–]\s*8)\b", body, re.I)


def test_it_tells_the_agent_to_record_the_rationale():
    body = SKILL.read_text()
    hits = [p for p in _paragraphs(body) if "rationale" in p.lower()]
    assert hits, "nothing tells the agent to record why the vocabulary looks like this"
    assert any("graph_upsert" in p for p in hits), \
        "the rationale has to be written into the graph, not said once in chat"


def _domain_nouns() -> list[str]:
    """The engine's forbidden-vocabulary list, read out of test_genericity.py by path so the two
    lists cannot drift and neither test depends on the other being importable."""
    import ast
    src = pathlib.Path(__file__).with_name("test_genericity.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "DOMAIN_NOUNS":
            return ast.literal_eval(node.value)
    raise AssertionError("DOMAIN_NOUNS is gone from test_genericity.py — update this test")


def test_the_skill_teaches_the_framework_not_a_domain():
    """Same rule as the engine (test_genericity): the skill ships to every deployment, so a
    borrowed example noun becomes a vocabulary suggestion nobody asked for."""
    text = (SKILL.read_text() + TRAITS.read_text()).lower()
    assert [n for n in _domain_nouns() if n.lower() in text] == []


def test_the_slash_command_offers_the_three_schema_modes():
    body = COMMAND.read_text()
    for mode in ("inherit", "interview", "bare"):
        assert f"`{mode}`" in body, f"/hivemind:project must offer schema={mode}"
    assert "hivemind-schema" in body
    assert re.search(r"load .{0,40}hivemind-schema", body, re.I | re.S), \
        "choosing interview must lead to the skill actually being loaded"
