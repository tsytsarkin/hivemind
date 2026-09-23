"""`hivemind-admin` — operator CLI, run ON the server host (direct DB/file access, no network).
Mint tokens, create projects, apply packs, promote schema, merge guide, GC, reindex.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import guide, schemas, search
from .config import config
from .identity import set_identity
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


def _cli_identity():
    """Who is at the keyboard, as `cli:<shell-user>`.

    Every subcommand here is human-initiated, so its writes must not land in `legacy:unknown` —
    that sentinel is for the genuinely principal-less paths (startup seeding, reindex, GC sweeps),
    and lumping an operator in with them loses the one fact that is knowable. The `cli:` prefix
    cannot collide with a real username because identity.USERNAME_RE forbids `:`, the same
    property that makes `legacy:` safe. This is for AUTHORSHIP only: nothing under admin.py
    authorizes against the contextvar (project-share builds its own actor), and the role stays
    `member` so it carries no authority it did not already have from host access.
    """
    from .identity import Identity
    try:
        user = getpass.getuser()
    except Exception:
        # No passwd entry and no USER/LOGNAME in the environment (a bare container). Still not
        # legacy:unknown: a human ran this.
        user = "unknown"
    return Identity(user=f"cli:{user}", device="admin-cli")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hivemind-admin", description="Hivemind operator CLI")
    ap.add_argument("--project", default="default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("mint-token")
    t.add_argument("--user", help="username this token authenticates as (server-level identity)")
    t.add_argument("--device", default="?", help="machine label, kept beside the identity")
    t.add_argument("--role", default="member", choices=["member", "admin"])
    t.add_argument("--client-id", help="legacy: mint into a project's own tokens.json instead")
    sub.add_parser("list-tokens")
    sub.add_parser("list-projects")
    sub.add_parser("create-project")
    # Recovery from the box: the MCP tools are owner-only (an admin who could share could grant
    # themselves read access), so an owner who has lost their token has no other way back in. This
    # acts AS the project's own owner, on a host where direct file access already grants everything.
    psh = sub.add_parser("project-share"); psh.add_argument("project"); psh.add_argument("user")
    pun = sub.add_parser("project-unshare"); pun.add_argument("project"); pun.add_argument("user")
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
    orp = sub.add_parser("orphans"); orp.add_argument("--older-than-hours", type=int, default=0)
    sub.add_parser("reindex")
    sub.add_parser("embed")
    sub.add_parser("autolink")

    args = ap.parse_args(argv)
    # Before _registry()/_project(), which can create a project and seed its guide section — those
    # writes belong to the operator who asked for them too.
    set_identity(_cli_identity())
    reg = _registry()
    cfg = config()

    if args.cmd in ("project-share", "project-unshare"):
        from . import project_tools
        from .identity import Identity, IdentityStore
        project = reg.get(args.project)
        if project is None:
            print(f"error: unknown project {args.project!r}", file=sys.stderr)
            raise SystemExit(2)
        owner = project.meta.owner
        if not owner:
            print(f"error: {args.project!r} has no owner to act as "
                  f"(a shared project is readable by everyone already)", file=sys.stderr)
            raise SystemExit(2)
        actor = Identity(user=owner, device="admin-cli")
        if args.cmd == "project-share":
            # identities is passed so a typo'd username is refused here: a share is silent until the
            # grantee calls, so granting a user who does not exist looks like it worked.
            _out(project_tools.share(reg, actor, args.project, args.user,
                                     identities=IdentityStore(cfg.identities_path)))
        else:
            _out(project_tools.unshare(reg, actor, args.project, args.user))
        return 0

    if args.cmd == "list-projects":
        _out({"projects": [p.name for p in reg.all()], "root": str(reg.root)}); return 0

    if args.cmd == "mint-token":
        if args.user:
            # Server-level identity: does NOT touch/create any project, unlike every other
            # subcommand below (which resolves --project and creates it if missing).
            from .identity import IdentityStore
            store = IdentityStore(cfg.identities_path)
            print(store.mint(args.user, args.device, args.role))
            return 0
        # legacy path, unchanged: a project-scoped token
        p = _project(reg, args.project)
        print(p.tokens.mint(args.client_id or "client"))
        return 0

    p = _project(reg, args.project)

    if args.cmd == "list-tokens":
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
    elif args.cmd == "orphans":
        _out(p.blobs.orphans(older_than_hours=args.older_than_hours))
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
    elif args.cmd == "autolink":
        from . import registry as _reg, skills as _sk
        _out({"skills": _sk.autolink_all(p.db), "tools": _reg.autolink_all(p.db)})
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
