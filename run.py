"""Single entry point for the pipeline. Eventual cron target.

    python reels-catalog/run.py scrape [--limit N] [--delay S] [--source ...]
    python reels-catalog/run.py enrich [--delay S] [--limit N]
    python reels-catalog/run.py categorize
    python reels-catalog/run.py tags
    python reels-catalog/run.py build
    python reels-catalog/run.py all
"""

import argparse
import os

DB = os.environ.get("REELS_DB", "reels.db")


def cmd_scrape(args):
    import scrape

    srcs = ("saved", "dm") if args.source == "both" else (args.source,)
    scrape.run(db_path=DB, limit=args.limit, delay=args.delay, sources=srcs)


def cmd_thread(args):
    import scrape

    scrape.run_thread(db_path=DB, thread_id=args.thread_id,
                      max_reels=args.max, delay=args.delay)


def cmd_enrich(args):
    import enrich

    enrich.run(db_path=DB, delay=args.delay, limit=getattr(args, "limit", None))


def cmd_categorize(args):
    import categorize

    categorize.run(db_path=DB)


def cmd_tags(args):
    import categorize

    categorize.run_tags(db_path=DB)


def cmd_build(args):
    import build_site

    build_site.build(db_path=DB)


def cmd_all(args):
    cmd_scrape(args)
    cmd_enrich(args)
    cmd_categorize(args)
    cmd_tags(args)
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

    tp = sub.add_parser("thread")
    tp.add_argument("--thread-id", required=True, dest="thread_id")
    tp.add_argument("--max", type=int, default=300)
    tp.add_argument("--delay", type=float, default=1.0)

    sub.add_parser("categorize")
    sub.add_parser("tags")
    sub.add_parser("build")

    args = p.parse_args(argv)
    {"scrape": cmd_scrape, "thread": cmd_thread, "enrich": cmd_enrich,
     "categorize": cmd_categorize, "tags": cmd_tags,
     "build": cmd_build, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    main()
