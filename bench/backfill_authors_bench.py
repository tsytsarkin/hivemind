#!/usr/bin/env python3
"""Re-derive the timings quoted in `admin._fill_null` / `admin._BATCH`.

The numbers in those comments came from this script. They are about how long a bulk UPDATE over
the authorship columns holds SQLite's single writer lock, which is the whole reason
`backfill-authors` writes in batches instead of one statement per table.

    python bench/backfill_authors_bench.py build   [db]        # ~8 GB proxy, a few minutes
    python bench/backfill_authors_bench.py sweep   <db> single
    python bench/backfill_authors_bench.py sweep   <db> batched [rows-per-batch]
    python bench/backfill_authors_bench.py blank   <db>        # re-NULL every author column

The proxy carries the LIVE row counts (see COUNTS) with every authorship column NULL. Run each
arm on its own copy — on APFS `cp -c src dst` clones instantly — or `blank` in between.

Recorded on a Mac Studio, 2026-09-23, proxy at 7.95 GB:

    dry run (all seven counts)   0.011 s     would_update 555,082
    single statement             83.0 s      380,729 rows; WAL grew to 7.85 GB
    batched, 5,000               81.1 s      78 batches; min 0.00 s, mean 1.04 s, max 2.50 s

with a second PROCESS attempting a small write every 200 ms at busy_timeout=250 throughout:
single = 5 in, then 147 consecutive refusals; batched = 87 in / 89 refused, alternating.

What those numbers do NOT establish, so nobody over-reads them:

* `props` here are a uniform ~20 KB. Per-batch time is driven by payload size, because SQLite
  rebuilds a row's whole record (overflow pages included) on UPDATE. The live size distribution
  is unknown, so per-batch time will vary more there; tune `batch` if it matters.
* The arms were NOT counterbalanced: single ran first on a freshly built file, batched afterwards
  on a clone. Both ran against a warm page cache, so both are lower bounds, and "batched is no
  slower" compares differently-warmed arms. The direction is benign (the later arm is the
  favoured one and still did not win), but it is not a controlled comparison.
* Four of the seven tables (skill_version, trap, tool_version, guide_proposal) are EMPTY here, so
  the 0.011 s dry run says nothing about them. They carry no index on author_user, so each batch
  scans them; that is fine at their real size and would not be if one ever grew.
* edge_version at 50,000 is a guess. Only node/node_version/tx counts come from the live server.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/hivemind-server/src"))

import sqlite3                                                              # noqa: E402
from hivemind_server import admin                                           # noqa: E402
from hivemind_server.db import Database                                     # noqa: E402

DEFAULT_DB = "/tmp/hivemind-backfill-bench/proxy.db"
# Live counts, measured on the server during task 8 of the identity work.
COUNTS = {"tx": 1_500_000, "node": 124_353, "node_version": 380_729, "edge_version": 50_000}
PROPS_BYTES = 20_000
AGENTS = ["atlas-migration", "cli", "laptop", "B6-FENCECENSUS", "verify-1.0.0", "agent",
          "import-job", "nikt-fleet"]
HEAD = 9223372036854775807


def build(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(path + suffix).unlink(missing_ok=True)
    t0 = time.time()
    Database(path).conn().close()        # real schema + migrations, then let go of the WAL lock
    con = sqlite3.connect(path, isolation_level=None)
    for pragma in ("journal_mode=OFF", "synchronous=OFF", "foreign_keys=OFF", "cache_size=-200000"):
        con.execute(f"PRAGMA {pragma}")
    n_tx, n_node, n_nv, n_ev = (COUNTS[k] for k in ("tx", "node", "node_version", "edge_version"))

    con.execute("BEGIN")
    con.executemany("INSERT INTO tx(tx_id,tx_time,agent_id,reason,meta) VALUES(?,?,?,?,'{}')",
                    ((i, "2026-01-01T00:00:00+00:00", AGENTS[i % len(AGENTS)], "seed")
                     for i in range(1, n_tx + 1)))
    con.executemany(
        "INSERT INTO node(node_id,node_type,created_by,created_tx) VALUES(?,'component',NULL,?)",
        ((f"n{i:07d}", (i * 7) % n_tx + 1) for i in range(n_node)))
    con.execute("COMMIT")
    print(f"tx {n_tx} + node {n_node} in {time.time() - t0:.0f}s", flush=True)

    props = json.dumps({"title": "x", "blob": "q" * PROPS_BYTES})
    con.execute("BEGIN")
    for chunk in range(0, n_nv, 20_000):
        con.executemany(
            "INSERT INTO node_version(version_id,node_id,seq,props,schema_ver,content_hash,"
            "author_user,tx_from,tx_to) VALUES(?,?,?,?,1,?,NULL,?,?)",
            # ux_node_head is a partial unique index on (node_id) WHERE tx_to = HEAD, so only one
            # version per node may be open; the rest are superseded, as on the live graph.
            ((f"v{i:07d}", f"n{i % n_node:07d}", i // n_node + 1, props, f"h{i:07d}",
              (i * 13) % n_tx + 1, HEAD if i < n_node else (i * 13) % n_tx + 2)
             for i in range(chunk, min(chunk + 20_000, n_nv))))
    con.execute("COMMIT")

    con.execute("BEGIN")
    con.executemany("INSERT INTO edge(edge_id,edge_type,src_node_id,dst_node_id,created_tx) "
                    "VALUES(?,'refines',?,?,?)",
                    ((f"e{i:07d}", f"n{i % n_node:07d}", f"n{(i * 3) % n_node:07d}",
                      (i * 5) % n_tx + 1) for i in range(n_ev)))
    con.executemany(
        "INSERT INTO edge_version(version_id,edge_id,seq,props,schema_ver,content_hash,"
        "author_user,tx_from,tx_to) VALUES(?,?,1,'{}',1,?,NULL,?,?)",
        ((f"ev{i:07d}", f"e{i:07d}", f"eh{i:07d}", (i * 11) % n_tx + 1, HEAD)
         for i in range(n_ev)))
    con.execute("COMMIT")
    con.close()
    print(f"{os.path.getsize(path) / 1e9:.2f} GB in {time.time() - t0:.0f}s", flush=True)


def blank(path):
    """Put every authorship column back the way a pre-identity row has it, to re-run an arm."""
    db = Database(path)
    with db.write_light() as cur:
        for table, column, _ in admin._AUTHOR_COLUMNS:
            cur.execute(f"UPDATE {table} SET {column}=NULL")
    print({t: c for t, c, _ in admin._AUTHOR_COLUMNS}, "blanked", flush=True)


def watch(path):
    """Another PROCESS, trying a small write every 200 ms: does the sweep lock the fleet out?"""
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA busy_timeout=250")
    while True:
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO meta(key,value) VALUES('bench-watch',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
            con.execute("COMMIT")
            sys.stdout.write("o")
        except sqlite3.OperationalError:
            try:
                con.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            sys.stdout.write("B")
        sys.stdout.flush()
        time.sleep(0.2)


def sweep(path, mode, batch):
    db = Database(path)
    with db.read() as cur:
        plan = [tuple(r)[3] for r in cur.execute(
            "EXPLAIN QUERY PLAN UPDATE node_version SET author_user='x' WHERE rowid IN "
            "(SELECT rowid FROM node_version WHERE author_user IS NULL LIMIT 5000)")]
    print("plan:", plan, flush=True)
    t0 = time.time()
    report = admin.backfill_authors(db, dry_run=True)
    print(f"dry run {time.time() - t0:.3f}s would_update={report['would_update']}", flush=True)

    watcher = subprocess.Popen([sys.executable, __file__, "watch", path])
    time.sleep(1.0)
    print(f"\n-- {mode} --", flush=True)
    t0 = time.time()
    if mode == "single":
        with db.write_light() as cur:
            cur.execute("UPDATE node_version SET author_user = "
                        + admin._legacy_author_sql("node_version.tx_from")
                        + " WHERE author_user IS NULL")
            rows = cur.rowcount
        print(f"\nSINGLE: {rows} rows in {time.time() - t0:.1f}s", flush=True)
    else:
        each = []
        sql = ("UPDATE node_version SET author_user = "
               + admin._legacy_author_sql("node_version.tx_from")
               + " WHERE rowid IN (SELECT rowid FROM node_version WHERE author_user IS NULL "
                 "LIMIT ?)")
        rows = 0
        while True:
            b0 = time.time()
            with db.write_light() as cur:
                cur.execute(sql, (batch,))
                changed = cur.rowcount
            each.append(time.time() - b0)
            if changed <= 0:
                break
            rows += changed
        print(f"\nBATCHED({batch}): {rows} rows in {time.time() - t0:.1f}s over {len(each)} "
              f"batches; min {min(each):.2f}s mean {sum(each) / len(each):.2f}s "
              f"max {max(each):.2f}s", flush=True)
    watcher.terminate()
    wal = Path(path + "-wal")
    print(f"WAL {wal.stat().st_size / 1e9:.2f} GB" if wal.exists() else "no WAL", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    db_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB
    if cmd == "build":
        build(db_path)
    elif cmd == "blank":
        blank(db_path)
    elif cmd == "watch":
        watch(db_path)
    elif cmd == "sweep":
        sweep(db_path, sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 5000)
    else:
        raise SystemExit(__doc__)
