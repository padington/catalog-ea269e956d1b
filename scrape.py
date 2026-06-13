"""Ingest reels shared in Instagram DMs into the SQLite DB.

instagrapi's typed models crash on Instagram's current "XMA" share format
(shared reels carry an `instagram://` video_url that fails URL validation), so
we read the direct inbox as RAW JSON via the private API and extract the share
attachments ourselves. Re-runs are resumable: db.upsert_reel does INSERT OR
IGNORE on the media pk.

A shared reel in a DM is an item whose `item_type` is one of SHARE_TYPES. The
attachment lives under that same key as a single-element list; the useful fields
are target_url (reel link + media id), header_title_text (creator handle),
preview_url (thumbnail), title_text/caption_body_text (caption, when present).
The stable media id is the item-level `original_media_igid`.
"""

import argparse
import os
import re
import time

import db as dbm

SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ig_session.json")

SHARE_TYPES = ("xma_clip", "xma_reel_share", "xma_media_share", "clip", "media_share")
SHORTCODE_RE = re.compile(r"/(?:reels?|p|tv)/([A-Za-z0-9_-]+)")


def load_client():
    from instagrapi import Client

    cl = Client()
    cl.load_settings(SESSION_PATH)
    return cl


def _payload(item, itype):
    p = item.get(itype)
    if isinstance(p, list):
        return p[0] if p else {}
    return p or {}


def _parse_target(target_url):
    if not target_url:
        return None, None
    clean = target_url.split("?", 1)[0]
    m = SHORTCODE_RE.search(target_url)
    return (m.group(1) if m else None), clean


def _thread_user_map(thread, viewer_id, viewer_name):
    users = {str(viewer_id): viewer_name}
    for u in thread.get("users", []):
        users[str(u.get("pk"))] = u.get("username")
    return users


def _extract_item(item, itype, user_map):
    p = _payload(item, itype)
    shortcode, clean_url = _parse_target(p.get("target_url"))
    if not clean_url or not shortcode:
        return None  # placeholders / expired shares carry no target_url

    pk = item.get("original_media_igid") or p.get("preview_media_fbid")
    if pk is None:
        return None

    caption = p.get("title_text") or p.get("caption_body_text")
    author = p.get("header_title_text")
    if not caption:
        caption = f"Reel by @{author}" if author else None

    return {
        "pk": str(pk),
        "shortcode": shortcode,
        "url": clean_url,
        "source": "dm",
        "shared_by": user_map.get(str(item.get("user_id"))),
        "caption": caption,
        "thumbnail_url": p.get("preview_url"),
        "taken_at": int(item.get("timestamp", 0) or 0) // 1_000_000 or None,
    }


def scrape_dms(cl, conn, limit, delay, thread_limit=20):
    viewer_id = cl.user_id
    viewer_name = getattr(cl, "username", None) or "me"

    res = cl.private_request(
        "direct_v2/inbox/",
        params={"persistentBadging": "true", "limit": str(thread_limit)},
    )
    threads = res.get("inbox", {}).get("threads", [])
    print(f"dm: scanning {len(threads)} thread(s)")

    count = 0
    for thread in threads:
        user_map = _thread_user_map(thread, viewer_id, viewer_name)
        for item in thread.get("items", []):
            itype = item.get("item_type")
            if itype not in SHARE_TYPES:
                continue
            try:
                row = _extract_item(item, itype, user_map)
            except Exception as exc:
                print(f"  skip dm item: {exc}")
                continue
            if row is None:
                continue
            dbm.upsert_reel(conn, row)
            count += 1
            if limit and count >= limit:
                print(f"dm: ingested {count} (limit reached)")
                return
        time.sleep(delay)
    print(f"dm: ingested {count}")


def scrape_saved(cl, conn, limit, delay):
    # Saved feed lives in collections; the default "All Posts" collection name
    # does not always resolve. Best-effort, never crash the run.
    try:
        pk = cl.collection_pk_by_name("All Posts")
        medias = cl.collection_medias(pk, amount=limit)
    except Exception as exc:
        print(f"saved: skipped ({type(exc).__name__}) - not wired for this account yet")
        return
    count = 0
    for media in medias:
        try:
            dbm.upsert_reel(conn, {
                "pk": str(media.pk),
                "shortcode": media.code,
                "url": f"https://www.instagram.com/reel/{media.code}/",
                "source": "saved",
                "shared_by": None,
                "caption": media.caption_text,
                "thumbnail_url": str(media.thumbnail_url) if media.thumbnail_url else None,
                "taken_at": int(media.taken_at.timestamp()) if media.taken_at else None,
            })
            count += 1
        except Exception as exc:
            print(f"  skip saved item: {exc}")
        time.sleep(delay)
    print(f"saved: ingested {count}")


def scrape_thread(cl, conn, thread_id, max_reels=300, delay=1.0, page_size=20):
    # Walk one DM thread oldest-ward via cursor pagination, collecting shared
    # reels (both directions: every item in the thread, regardless of sender)
    # until max_reels or the thread runs out. Resumable via INSERT OR IGNORE.
    viewer_id = cl.user_id
    viewer_name = getattr(cl, "username", None) or "me"

    cursor = None
    collected = 0
    pages = 0
    while True:
        params = {"limit": str(page_size)}
        if cursor:
            params["cursor"] = cursor
            params["direction"] = "older"
        res = cl.private_request(f"direct_v2/threads/{thread_id}/", params=params)
        thread = res.get("thread", {})
        user_map = _thread_user_map(thread, viewer_id, viewer_name)
        pages += 1

        for item in thread.get("items", []):
            if item.get("item_type") not in SHARE_TYPES:
                continue
            try:
                row = _extract_item(item, item["item_type"], user_map)
            except Exception as exc:
                print(f"  skip item: {exc}")
                continue
            if row is None:
                continue
            dbm.upsert_reel(conn, row)
            collected += 1
            if collected >= max_reels:
                print(f"thread {thread_id}: ingested {collected} reel(s) over {pages} page(s) (max reached)")
                return collected

        if not thread.get("has_older"):
            break
        cursor = thread.get("oldest_cursor")
        if not cursor:
            break
        time.sleep(delay)

    print(f"thread {thread_id}: ingested {collected} reel(s) over {pages} page(s)")
    return collected


def run_thread(db_path, thread_id, max_reels=300, delay=1.0):
    conn = dbm.connect(db_path)
    dbm.init_db(conn)
    cl = load_client()
    return scrape_thread(cl, conn, thread_id, max_reels=max_reels, delay=delay)


def run(db_path="reels.db", limit=50, delay=2.0, sources=("dm",)):
    conn = dbm.connect(db_path)
    dbm.init_db(conn)
    cl = load_client()
    if "dm" in sources:
        scrape_dms(cl, conn, limit, delay)
    if "saved" in sources:
        scrape_saved(cl, conn, limit, delay)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Scrape Instagram DM reels into the DB")
    p.add_argument("--db", default=os.environ.get("REELS_DB", "reels.db"))
    p.add_argument("--limit", type=int, default=50, help="max reels to ingest")
    p.add_argument("--delay", type=float, default=2.0, help="seconds between threads")
    p.add_argument("--source", choices=["saved", "dm", "both"], default="dm")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    srcs = ("dm", "saved") if args.source == "both" else (args.source,)
    run(db_path=args.db, limit=args.limit, delay=args.delay, sources=srcs)
