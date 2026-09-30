"""Backlog: reels.db (Mac) -> batches on the VPS runner -> libinsta channel.

SPEC §4 iteration 2. One cycle:
  1. select pks with tg_message_id IS NULL (newest first), skipping ones whose
     last outcome is final for the current downloader (no_video/unavailable);
  2. write batches/current.json into a tgbase checkout, commit + push to
     probe/ig-net;
  3. `gh workflow run ig-download.yml` (workflow_dispatch; pushes of batches/
     no longer trigger it), wait for the run;
  4. download artifact results-<run_id> (results.jsonl), write outcomes to
     reels.db (§3.3 columns + tg_status/tg_error);
  5. on `throttled`: pause >= 1 h, DELAY x1.5.

Safety: default is ONE batch; looping needs --all (full run only with the
owner's OK). Batch size is capped at 150 (SPEC §5).

Usage (from the catalog checkout, Mac):
  PYTHONPATH=. python backlog.py run   --db ~/reels-catalog/reels.db --size 30
  PYTHONPATH=. python backlog.py run   --db ... --all            # full backlog
  PYTHONPATH=. python backlog.py apply --db ... --run-id 123      # re-import a run
  PYTHONPATH=. python backlog.py mark  --db ... --pk P --message-id 7 [--file-id F]
  PYTHONPATH=. python backlog.py report --db ... [--out backlog_report.md]
  PYTHONPATH=. python backlog.py plan  --db ... --size 30         # dry: print batch
"""
import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import db as dbm

MAX_BATCH = 150
TGBASE_REPO = "padington/tgbase"
TGBASE_REF = "probe/ig-net"
WORKFLOW = "ig-download.yml"
BATCH_PATH = "batches/current.json"
# Outcomes after which the pk is NOT re-selected by default. no_video (photo
# carousels) and unavailable wait for iteration 1's post_media (§3.2).
FINAL = ("sent", "manual", "no_video", "unavailable")
THROTTLE_PAUSE = 3600


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


# ---------------------------------------------------------------- pure logic

def select_batch(conn, size, retry=()):
    """Newest-first pks not yet in the channel. `retry` re-admits statuses
    from FINAL (e.g. ('no_video',) once photos are supported)."""
    size = max(0, min(int(size), MAX_BATCH))
    skip = [s for s in FINAL if s not in retry]
    q = ("SELECT pk, shortcode, shared_by, caption FROM reels "
         "WHERE tg_message_id IS NULL")
    if skip:
        q += " AND (tg_status IS NULL OR tg_status NOT IN (%s))" % ",".join("?" * len(skip))
    q += " ORDER BY taken_at DESC, created_at DESC, pk DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, (*skip, size))]


def batch_payload(rows):
    return [{"pk": str(r["pk"]), "shortcode": r.get("shortcode") or "",
             "shared_by": r.get("shared_by") or "",
             "caption": (r.get("caption") or "")[:1000]} for r in rows]


def parse_results(text):
    """results.jsonl -> {pk: record}; last line per pk wins, junk ignored."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("pk"):
            out[str(rec["pk"])] = rec
    return out


def kind_of(rec):
    if rec.get("kind") in ("video", "photos", "text"):
        return rec["kind"]
    st = rec.get("status")
    if st == "sent":
        return "video"
    if st == "photos":
        return "photos"
    return None


def apply_results(conn, records, now=None):
    """Write outcomes. A message_id is written only for posted records; an
    already-posted row is never overwritten by a later failure."""
    now = int(now if now is not None else time.time())
    counts = {}
    for pk, rec in records.items():
        st = rec.get("status") or "failed"
        counts[st] = counts.get(st, 0) + 1
        mid = rec.get("message_id")
        if mid:
            conn.execute(
                "UPDATE reels SET tg_message_id=?, tg_file_id=?, tg_kind=?, "
                "tg_posted_at=?, tg_status=?, tg_error=NULL WHERE pk=?",
                (int(mid), rec.get("file_id"), kind_of(rec) or "video", now, st, pk))
        else:
            conn.execute(
                "UPDATE reels SET tg_status=?, tg_error=? WHERE pk=? AND tg_message_id IS NULL",
                (st, (rec.get("error") or "")[:500] or None, pk))
    conn.commit()
    return counts


def mark_posted(conn, pk, message_id, file_id=None, kind="video", status="manual", now=None):
    cur = conn.execute(
        "UPDATE reels SET tg_message_id=?, tg_file_id=?, tg_kind=?, tg_posted_at=?, "
        "tg_status=?, tg_error=NULL WHERE pk=?",
        (int(message_id), file_id, kind, int(now if now is not None else time.time()), status, str(pk)))
    conn.commit()
    return cur.rowcount


def next_delay(delay, throttled):
    return round(delay * 1.5, 2) if throttled else delay


def summary(conn):
    total = conn.execute("SELECT COUNT(*) FROM reels").fetchone()[0]
    posted = conn.execute("SELECT COUNT(*) FROM reels WHERE tg_message_id IS NOT NULL").fetchone()[0]
    by = {r[0] or "pending": r[1] for r in conn.execute(
        "SELECT tg_status, COUNT(*) FROM reels GROUP BY tg_status")}
    kinds = {r[0]: r[1] for r in conn.execute(
        "SELECT tg_kind, COUNT(*) FROM reels WHERE tg_message_id IS NOT NULL GROUP BY tg_kind")}
    return {"total": total, "posted": posted, "by_status": by, "by_kind": kinds}


def report_md(conn, now=None):
    s = summary(conn)
    lines = ["# Backlog report", "",
             "Generated: %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(now or time.time())), "",
             "Total in reels.db: **%d**, in channel: **%d**" % (s["total"], s["posted"]), "",
             "| status | count |", "|---|---|"]
    for k in sorted(s["by_status"]):
        lines.append("| %s | %d |" % (k, s["by_status"][k]))
    lines += ["", "| tg_kind | count |", "|---|---|"]
    for k in sorted(s["by_kind"], key=str):
        lines.append("| %s | %d |" % (k, s["by_kind"][k]))
    bad = list(conn.execute(
        "SELECT pk, shortcode, tg_status, tg_error FROM reels WHERE tg_message_id IS NULL "
        "AND tg_status IS NOT NULL ORDER BY tg_status, taken_at DESC"))
    lines += ["", "## Not posted (with reason)", ""]
    if not bad:
        lines.append("none")
    for r in bad:
        lines.append("- `%s` [%s](https://www.instagram.com/reel/%s/) — %s%s" % (
            r[0], r[1], r[1], r[2], (": " + r[3].replace("\n", " ")[:200]) if r[3] else ""))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- side effects

def sh(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if check and p.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (" ".join(args[:3]), p.returncode, p.stdout[-800:]))
    return p.stdout


def open_db(path):
    conn = dbm.connect(path)
    if dbm.missing_tg_columns(conn):
        conn.close()
        bak = dbm.backup_db(path)
        log("backup before migration: %s" % bak)
        conn = dbm.connect(path)
        log("added columns: %s" % dbm.migrate_tg_columns(conn))
    return conn


def ensure_tgbase(path):
    if not os.path.isdir(os.path.join(path, ".git")):
        sh(["gh", "repo", "clone", TGBASE_REPO, path, "--", "-b", TGBASE_REF])
    sh(["git", "fetch", "-q", "origin", TGBASE_REF], cwd=path)
    sh(["git", "checkout", "-q", TGBASE_REF], cwd=path)
    sh(["git", "reset", "-q", "--hard", "origin/" + TGBASE_REF], cwd=path)


def push_batch(tgbase, payload, note):
    ensure_tgbase(tgbase)  # other work lands on the same branch — resync before every batch
    with open(os.path.join(tgbase, BATCH_PATH), "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    sh(["git", "add", BATCH_PATH], cwd=tgbase)
    if not sh(["git", "status", "--porcelain", BATCH_PATH], cwd=tgbase).strip():
        return sh(["git", "rev-parse", "HEAD"], cwd=tgbase).strip()
    sh(["git", "-c", "user.name=libinsta-backlog", "-c", "user.email=libinsta-backlog@local",
        "commit", "-q", "-m", "backlog batch: %s" % note], cwd=tgbase)
    for attempt in range(3):
        try:
            sh(["git", "push", "-q", "origin", "HEAD:" + TGBASE_REF], cwd=tgbase)
            break
        except RuntimeError as exc:
            if "rejected" not in str(exc) or attempt == 2:
                raise
            log("push rejected (concurrent commit) — rebasing and retrying")
            sh(["git", "fetch", "-q", "origin", TGBASE_REF], cwd=tgbase)
            sh(["git", "rebase", "-q", "origin/" + TGBASE_REF], cwd=tgbase)
    return sh(["git", "rev-parse", "HEAD"], cwd=tgbase).strip()


def dispatch(delay, tag, catalog_ref):
    since = time.time() - 5
    out = sh(["gh", "workflow", "run", WORKFLOW, "--repo", TGBASE_REPO, "--ref", TGBASE_REF,
              "-f", "delay=%s" % delay, "-f", "tag=%s" % tag, "-f", "catalog_ref=%s" % catalog_ref,
              "-f", "batch=%s" % BATCH_PATH])
    m = re.search(r"/actions/runs/(\d+)", out)
    if m:
        return m.group(1)
    for _ in range(30):
        time.sleep(4)
        runs = json.loads(sh(["gh", "run", "list", "--repo", TGBASE_REPO, "--workflow", WORKFLOW,
                              "--event", "workflow_dispatch", "-L", "5",
                              "--json", "databaseId,createdAt"]))
        for r in runs:
            ts = calendar.timegm(time.strptime(r["createdAt"], "%Y-%m-%dT%H:%M:%SZ"))
            if ts >= since:
                return str(r["databaseId"])
    raise RuntimeError("dispatched run not found")


def wait_run(run_id, poll=30):
    while True:
        st = json.loads(sh(["gh", "run", "view", run_id, "--repo", TGBASE_REPO,
                            "--json", "status,conclusion"]))
        if st["status"] == "completed":
            return st["conclusion"]
        time.sleep(poll)


def fetch_results(run_id):
    d = tempfile.mkdtemp(prefix="backlog-%s-" % run_id)
    sh(["gh", "run", "download", run_id, "--repo", TGBASE_REPO, "-n", "results-%s" % run_id, "-D", d])
    p = os.path.join(d, "results.jsonl")
    with open(p) as f:
        return f.read()


def run_one(conn, args, delay):
    rows = select_batch(conn, args.size, tuple(args.retry or ()))
    if not rows:
        return None, {}
    payload = batch_payload(rows)
    log("batch %d pks: %s .. %s" % (len(payload), payload[0]["pk"], payload[-1]["pk"]))
    sha = push_batch(args.tgbase, payload, "%d pks, newest %s" % (len(payload), payload[0]["pk"]))
    log("pushed %s, dispatching (delay=%s)" % (sha[:8], delay))
    run_id = dispatch(delay, args.tag, args.catalog_ref)
    log("run %s: https://github.com/%s/actions/runs/%s" % (run_id, TGBASE_REPO, run_id))
    concl = wait_run(run_id)
    log("run %s finished: %s" % (run_id, concl))
    text = fetch_results(run_id)
    recs = parse_results(text)
    counts = apply_results(conn, recs)
    missing = [p["pk"] for p in payload if p["pk"] not in recs]
    log("applied %d results %s; no result for %d pk(s)" % (len(recs), counts, len(missing)))
    return run_id, counts


def cmd_run(conn, args):
    delay = args.delay
    ensure_tgbase(args.tgbase)
    n = 0
    while True:
        run_id, counts = run_one(conn, args, delay)
        if run_id is None:
            log("backlog empty")
            break
        n += 1
        throttled = bool(counts.get("throttled"))
        if not args.all or (args.max_batches and n >= args.max_batches):
            break
        if throttled:
            delay = next_delay(delay, True)
            log("throttled: sleeping %ds, delay -> %s" % (THROTTLE_PAUSE, delay))
            time.sleep(THROTTLE_PAUSE)
    print(json.dumps(summary(conn), ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["run", "plan", "apply", "mark", "report"])
    ap.add_argument("--db", default=os.path.expanduser("~/reels-catalog/reels.db"))
    ap.add_argument("--size", type=int, default=MAX_BATCH)
    ap.add_argument("--all", action="store_true", help="loop until the backlog is empty (owner OK needed)")
    ap.add_argument("--max-batches", type=int, default=0)
    ap.add_argument("--retry", action="append", help="re-select this final status (repeatable)")
    ap.add_argument("--delay", type=float, default=6)
    ap.add_argument("--tag", default="#nonparsed")
    ap.add_argument("--catalog-ref", default="vps-download")
    ap.add_argument("--tgbase", default=os.path.expanduser("~/.cache/libinsta-tgbase"))
    ap.add_argument("--run-id")
    ap.add_argument("--pk"); ap.add_argument("--message-id", type=int); ap.add_argument("--file-id")
    ap.add_argument("--kind", default="video")
    ap.add_argument("--out", default="backlog_report.md")
    args = ap.parse_args(argv)
    conn = open_db(args.db)
    if args.cmd == "plan":
        for p in batch_payload(select_batch(conn, args.size, tuple(args.retry or ()))):
            print(p["pk"], p["shortcode"], p["caption"][:50].replace("\n", " "))
    elif args.cmd == "run":
        cmd_run(conn, args)
    elif args.cmd == "apply":
        recs = parse_results(fetch_results(args.run_id))
        print(apply_results(conn, recs))
    elif args.cmd == "mark":
        print("rows updated:", mark_posted(conn, args.pk, args.message_id, args.file_id, args.kind))
    elif args.cmd == "report":
        with open(args.out, "w") as f:
            f.write(report_md(conn))
        print(json.dumps(summary(conn), ensure_ascii=False))


if __name__ == "__main__":
    main()
