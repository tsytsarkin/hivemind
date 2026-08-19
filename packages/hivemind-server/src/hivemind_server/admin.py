"""`hivemind-admin` — operator CLI, run ON the server host (direct DB/file access, no network).
Mint tokens, create projects, apply packs, promote schema, merge guide, GC, reindex.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import guide, schemas, search
from .config import config
from .project import ProjectRegistry, projects_root_from_env, valid_name


def _registry() -> ProjectRegistry:
    cfg = config()
    cfg.ensure_dirs()
    reg = ProjectRegistry(projects_root_from_env(cfg.data_dir),
                          max_blob_bytes=cfg.max_blob_bytes,
                          blob_grace_seconds=cfg.blob_grace_seconds)
    reg.discover()
    return reg


def _project(reg, name):
    p = reg.get(name) or (reg.create(name) if valid_name(name) else None)
    if p is None:
        print(f"error: unknown/invalid project {name!r}", file=sys.stderr)
        raise SystemExit(2)
    return p


def _out(o):
    print(json.dumps(o, indent=2, ensure_ascii=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hivemind-admin", description="Hivemind operator CLI")
    ap.add_argument("--project", default="default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("mint-token"); t.add_argument("--client-id", default="client")
    t.add_argument("--scope", action="append", dest="scopes")
    sub.add_parser("list-tokens")
    sub.add_parser("list-projects")
    sub.add_parser("create-project")
    ap_pack = sub.add_parser("apply-pack"); ap_pack.add_argument("pack_file")
    ap_pack.add_argument("--force", action="store_true",
                         help="allow a NON-ADDITIVE change to an existing type")
    pr = sub.add_parser("promote"); pr.add_argument("kind"); pr.add_argument("name")
    pr.add_argument("--version", type=int)
    sub.add_parser("list-proposals")
    mg = sub.add_parser("merge-guide"); mg.add_argument("proposal_id")
    sg = sub.add_parser("set-guide"); sg.add_argument("section"); sg.add_argument("file")
    rg = sub.add_parser("retire-guide"); rg.add_argument("section")
    rg.add_argument("--reason", default="")
    gc = sub.add_parser("gc"); gc.add_argument("--yes", action="store_true")
    sub.add_parser("reindex")
    sub.add_parser("embed")

    args = ap.parse_args(argv)
    reg = _registry()

    if args.cmd == "list-projects":
        _out({"projects": [p.name for p in reg.all()], "root": str(reg.root)}); return 0

    p = _project(reg, args.project)

    if args.cmd == "mint-token":
        tok = p.tokens.mint(args.client_id, args.scopes)
        _out({"project": p.name, "client_id": args.client_id, "token": tok,
              "note": "store securely; it is shown only once"})
    elif args.cmd == "list-tokens":
        data = json.loads((p.dir / "tokens.json").read_text()) if (p.dir / "tokens.json").exists() else {}
        _out({"project": p.name, "tokens": [{"token_prefix": k[:8] + "…", **v}
                                            for k, v in data.items()]})
    elif args.cmd == "create-project":
        _out({"created": p.name, "dir": str(p.dir)})
    elif args.cmd == "apply-pack":
        pack = json.loads(Path(args.pack_file).read_text())
        res = schemas.apply_pack(p.db, "admin", pack, force=args.force)
        # load any guide sections shipped in the pack dir
        loaded = _load_pack_guide(p, Path(args.pack_file))
        _out({**res, "guide_sections_loaded": loaded})
    elif args.cmd == "promote":
        _out(schemas.promote_type(p.db, "admin", args.kind, args.name, version=args.version))
    elif args.cmd == "list-proposals":
        _out(guide.list_proposals(p.db))
    elif args.cmd == "merge-guide":
        _out(guide.merge_proposal(p.db, "admin", args.proposal_id))
    elif args.cmd == "set-guide":
        _out(guide.set_section(p.db, "admin", args.section, Path(args.file).read_text()))
    elif args.cmd == "retire-guide":
        _out(guide.retire_section(p.db, "admin", args.section, args.reason))
    elif args.cmd == "gc":
        _out(p.blobs.gc(dry_run=not args.yes))
    elif args.cmd == "embed":
        import json as _json
        from . import embeddings, registry as _reg
        with p.db.read() as cur:
            sk = [(r["id"], " ".join(filter(None, [r["id"], r["title"], r["description"],
                                                    r["when_to_use"] or "", r["body"]])))
                  for r in cur.execute(
                      "SELECT sv.id, sv.title, sv.description, sv.when_to_use, sv.body "
                      "FROM skill s JOIN skill_version sv ON sv.id=s.id "
                      "AND sv.version=s.latest_version")]
            tl = []
            for r in cur.execute("SELECT t.id, tv.manifest FROM tool t JOIN tool_version tv "
                                 "ON tv.id=t.id AND tv.version=t.latest_version"):
                mm = _json.loads(r["manifest"])
                tl.append((r["id"], " ".join(filter(None, [
                    r["id"], mm.get("description", ""), " ".join(mm.get("tags") or []),
                    mm.get("runtime", "")]))))
        res = {"skills": embeddings.backfill(p.db, "skill", sk),
               "tools": embeddings.backfill(p.db, "tool", tl),
               "tool_fts_reindexed": _reg.reindex_all(p.db)}
        _out(res)
    elif args.cmd == "reindex":
        _out({"reindexed_nodes": search.reindex_all(p.db)})
    return 0


def _load_pack_guide(project, pack_file: Path) -> list:
    """If the pack dir has a guide/ folder of *.md, load each as a guide section."""
    gdir = pack_file.parent / "guide"
    loaded = []
    if gdir.is_dir():
        for md in sorted(gdir.glob("*.md")):
            guide.set_section(project.db, "admin", md.stem, md.read_text())
            loaded.append(md.stem)
    return loaded


if __name__ == "__main__":
    raise SystemExit(main())
