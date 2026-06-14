"""Single entry point for the pipeline. Eventual cron target.

    python reels-catalog/run.py scrape [--limit N] [--delay S] [--source ...]
    python reels-catalog/run.py enrich [--delay S] [--limit N]
    python reels-catalog/run.py download [--limit N] [--delay S]
    python reels-catalog/run.py transcribe [--limit N] [--delay S]
    python reels-catalog/run.py categorize
    python reels-catalog/run.py tags
    python reels-catalog/run.py status
    python reels-catalog/run.py migrate
    python reels-catalog/run.py build
    python reels-catalog/run.py all

The per-reel stages (enrich/download/transcribe/categorize/tags) are now driven
by the explicit queue + generic driver in pipeline.py; each subcommand just
drains its stage.
"""

import argparse
import os

DB = os.environ.get("REELS_DB", "reels.db")


def _drain(stage, **kw):
    import db as dbm
    import pipeline

    conn = dbm.connect(DB)
    dbm.init_db(conn)
    ctx = pipeline.Context(conn)
    pipeline.drain(conn, stage, ctx, **kw)


def cmd_scrape(args):
    import scrape

    srcs = ("saved", "dm") if args.source == "both" else (args.source,)
    scrape.run(db_path=DB, limit=args.limit, delay=args.delay, sources=srcs)


def cmd_thread(args):
    import scrape

    scrape.run_thread(db_path=DB, thread_id=args.thread_id,
                      max_reels=args.max, delay=args.delay)


def cmd_enrich(args):
    _drain("enrich", limit=getattr(args, "limit", None),
           delay=getattr(args, "delay", 2.0))


def cmd_download(args):
    _drain("download", limit=getattr(args, "limit", None),
           delay=getattr(args, "delay", 6.0))


def cmd_transcribe(args):
    _drain("transcribe", limit=getattr(args, "limit", None),
           delay=getattr(args, "delay", 0.0))


def cmd_categorize(args):
    _drain("categorize")


def cmd_tags(args):
    _drain("tags")


def cmd_status(args):
    import db as dbm
    import pipeline

    conn = dbm.connect(DB)
    dbm.init_db(conn)
    pipeline.print_status(conn)


def cmd_migrate(args):
    """One-time lossless migration for a DB populated under the OLD pipeline.

    Seeds terminal queue rows from the content each reel already has (pure
    SQL/disk, ZERO network), then materialises pending rows via enqueue_ready
    in DAG order so `status` is immediately accurate. Idempotent: safe to
    re-run (INSERT OR IGNORE never resets an existing queue row).
    """
    import db as dbm
    import pipeline

    conn = dbm.connect(DB)
    dbm.init_db(conn)

    summary = dbm.backfill_queue(conn)
    print("backfill inserted:")
    if not summary:
        print("  (nothing — queue already seeded)")
    else:
        for (stage, status), n in sorted(summary.items()):
            print(f"  {stage:<12} {status:<8} {n}")

    for stage in ("enrich", "download", "transcribe", "categorize", "tags"):
        pipeline.enqueue_ready(conn, stage)

    print()
    pipeline.print_status(conn)


def cmd_build(args):
    import build_site

    build_site.build(db_path=DB)


def cmd_all(args):
    cmd_scrape(args)
    for stage in ("enrich", "download", "transcribe", "categorize", "tags"):
        _drain(stage,
               delay=getattr(args, "delay", 2.0) if stage in ("enrich", "download")
               else 0.0)
    cmd_build(args)


def main(argv=None):
    p = argparse.ArgumentParser(description="Reels catalogue pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("scrape", "all"):
        sp = sub.add_parser(name)
        sp.add_argument("--limit", type=int, default=50)
        sp.add_argument("--delay", type=float, default=2.0)
        sp.add_argument("--source", choices=["saved", "dm", "both"], default="both")

    ep = sub.add_parser("enrich")
    ep.add_argument("--delay", type=float, default=2.0)
    ep.add_argument("--limit", type=int, default=None)

    dp = sub.add_parser("download")
    dp.add_argument("--limit", type=int, default=None)
    dp.add_argument("--delay", type=float, default=6.0)

    xp = sub.add_parser("transcribe")
    xp.add_argument("--limit", type=int, default=None)
    xp.add_argument("--delay", type=float, default=0.0)

    tp = sub.add_parser("thread")
    tp.add_argument("--thread-id", required=True, dest="thread_id")
    tp.add_argument("--max", type=int, default=300)
    tp.add_argument("--delay", type=float, default=1.0)

    sub.add_parser("categorize")
    sub.add_parser("tags")
    sub.add_parser("status")
    sub.add_parser("migrate")
    sub.add_parser("build")

    args = p.parse_args(argv)
    {"scrape": cmd_scrape, "thread": cmd_thread, "enrich": cmd_enrich,
     "download": cmd_download, "transcribe": cmd_transcribe,
     "categorize": cmd_categorize, "tags": cmd_tags, "status": cmd_status,
     "migrate": cmd_migrate, "build": cmd_build, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    main()
