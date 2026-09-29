"""Local tagger for the libinsta channel (SPEC §4, iteration 3). Runs on the Mac.

    PYTHONPATH=. python parse_channel.py [--limit N] [--dry-run] [--only-index]
                                         [--mode bot|user] [--ids 9,10] [--scan-all]
                                         [--no-publish]

Flow per channel post whose last caption line is exactly `#nonparsed`:
  1. read the post over MTProto (Telethon). Bot mode (default): bots may not call
     messages.getHistory / messages.search (BotMethodInvalidError, checked
     30.09.2026), but channels.getMessages by id works, so we scan ids in chunks
     of 100 until a run of empty chunks. User mode (`tg_user.session`) uses
     `iter_messages(search='#nonparsed')`.
  2. pk from the instagram link in the caption; reels.db row is created
     (`source='link'`) if missing; media downloaded to media/<pk>.mp4
     (photos: media/<pk>/NN.jpg).
  3. local stages: extract_audio, transcribe, sample_frames, describe_frames,
     categorize, tags (queue rows marked done; stages already done are reused).
     Photos: only describe_frames over the photos themselves.
  4. tag_normalize against tags_tree.yaml -> hashtags; Bot API
     editMessageCaption replaces ONLY the last line; the returned caption is
     compared with what we sent.
  5. `tags_final`/`parsed_at` written to reels (if the columns exist — iteration
     2 adds them) and to our own table `tg_posts`; index.json rebuilt and pushed
     (index_build.py).

Idempotent: a post is picked only while its last line is `#nonparsed`; every
step is re-runnable; Ctrl-C between posts leaves consistent state.
--dry-run: no Telegram edits and no tags_final/parsed_at writes (the local
stages still run and persist their outputs, as they are idempotent).

Env: TELEGRAM_BOT_TOKEN, TG_API_ID, TG_API_HASH (from .env),
LIBINSTA_CHANNEL_ID (default -1004300487255), REELS_DB, REELS_MEDIA_DIR.
The Mac reaches Telegram DC1 only over IPv6, so Telethon runs with use_ipv6
(TG_USE_IPV6=0 to disable).
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import tag_normalize as tn

CHANNEL_ID = int(os.environ.get("LIBINSTA_CHANNEL_ID", "-1004300487255"))
NONPARSED = "#nonparsed"
CAPTION_LIMIT = 1024
HERE = os.path.dirname(os.path.abspath(__file__))
STAGE_ORDER = ("extract_audio", "transcribe", "sample_frames", "describe_frames",
               "categorize", "tags")


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested).
# --------------------------------------------------------------------------- #

def tg_len(text):
    """Caption length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def split_last_line(caption):
    """-> (head, last) where last is the last non-empty line (stripped) and
    head is everything before it (trailing newlines kept as-is)."""
    text = (caption or "").rstrip()
    i = text.rfind("\n")
    if i < 0:
        return "", text.strip()
    return text[:i + 1], text[i + 1:].strip()


def last_line(caption):
    return split_last_line(caption)[1]


def is_nonparsed(caption):
    return last_line(caption) == NONPARSED


def replace_last_line(caption, ids, limit=CAPTION_LIMIT):
    """Replace the last line with `#id #id ...`; everything above is unchanged.

    If the result would exceed `limit`, trailing hashtags are dropped (the first
    one — the category — is always kept); full tags stay in the DB/index.
    """
    head, _ = split_last_line(caption)
    ids = list(ids)
    while True:
        new = head + tn.hashtag_line(ids)
        if tg_len(new) <= limit or len(ids) <= 1:
            return new
        ids.pop()


_IG_URL = re.compile(r"https?://(?:www\.)?instagram\.com/(reel|reels|p|tv)/([A-Za-z0-9_-]+)")
_META = re.compile(r"^@([A-Za-z0-9._]+)\s*·\s*(\d{4}-\d{2}-\d{2})(?:\s*·\s*from\s+(.+))?\s*$")


def parse_caption(caption):
    """Parse a §3.1 caption -> dict(shortcode, url, author, date, shared_by, text)."""
    caption = caption or ""
    out = {"shortcode": None, "url": None, "author": None, "date": None,
           "shared_by": None, "text": ""}
    m = _IG_URL.search(caption)
    if m:
        out["shortcode"] = m.group(2)
        kind = "reel" if m.group(1) in ("reel", "reels") else m.group(1)
        out["url"] = "https://www.instagram.com/%s/%s/" % (kind, m.group(2))
    lines = caption.splitlines()
    for ln in lines:
        mm = _META.match(ln.strip())
        if mm:
            out["author"], out["date"], out["shared_by"] = mm.group(1), mm.group(2), mm.group(3)
            break
    # IG text = everything before the first line holding the instagram link
    text_lines = []
    for ln in lines:
        if _IG_URL.search(ln):
            break
        text_lines.append(ln)
    out["text"] = "\n".join(text_lines).strip()
    return out


_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def media_pk_from_code(code):
    """Instagram shortcode -> media pk (same as instagrapi.media_pk_from_code)."""
    code = code[:11] if len(code) > 28 else code
    pk = 0
    for ch in code:
        pk = pk * 64 + _B64.index(ch)
    return str(pk)


# --------------------------------------------------------------------------- #
# DB.
# --------------------------------------------------------------------------- #

TG_POSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_posts (
    message_id INTEGER PRIMARY KEY,
    pk         TEXT,
    kind       TEXT,
    author     TEXT,
    date       TEXT,
    caption    TEXT,
    tags       TEXT,
    parsed_at  INTEGER
)
"""


def init_tg_posts(conn):
    conn.execute(TG_POSTS_SCHEMA)
    conn.commit()


def reels_cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(reels)")}


def upsert_tg_post(conn, message_id, **fields):
    init_tg_posts(conn)
    conn.execute("INSERT OR IGNORE INTO tg_posts (message_id) VALUES (?)", (message_id,))
    for k, v in fields.items():
        if k not in ("pk", "kind", "author", "date", "caption", "tags", "parsed_at"):
            raise ValueError(k)
        conn.execute("UPDATE tg_posts SET %s = ? WHERE message_id = ?" % k, (v, message_id))
    conn.commit()


def ensure_reel(conn, meta, message_id, kind):
    """Find/create the reels row for a channel post; returns pk."""
    import db as dbm
    row = None
    if meta.get("shortcode"):
        row = conn.execute("SELECT pk FROM reels WHERE shortcode = ?",
                           (meta["shortcode"],)).fetchone()
    if row is not None:
        pk = row[0]
    else:
        pk = media_pk_from_code(meta["shortcode"])
        taken = None
        if meta.get("date"):
            taken = int(time.mktime(time.strptime(meta["date"], "%Y-%m-%d")))
        dbm.upsert_reel(conn, {"pk": pk, "shortcode": meta["shortcode"], "url": meta["url"],
                               "source": "link", "shared_by": meta.get("shared_by"),
                               "caption": meta.get("text") or None, "taken_at": taken})
    cols = reels_cols(conn)
    r = conn.execute("SELECT caption FROM reels WHERE pk = ?", (pk,)).fetchone()
    if meta.get("text") and (r[0] is None or r[0].startswith("Reel by @")):
        conn.execute("UPDATE reels SET caption = ? WHERE pk = ?", (meta["text"], pk))
    if "tg_message_id" in cols:
        conn.execute("UPDATE reels SET tg_message_id = COALESCE(tg_message_id, ?) WHERE pk = ?",
                     (message_id, pk))
    if "tg_kind" in cols:
        conn.execute("UPDATE reels SET tg_kind = COALESCE(tg_kind, ?) WHERE pk = ?", (kind, pk))
    conn.commit()
    return pk


def queue_status(conn, pk, stage):
    r = conn.execute("SELECT status FROM queue WHERE pk = ? AND stage = ?", (pk, stage)).fetchone()
    return r[0] if r else None


def set_queue(conn, pk, stage, status, error=None):
    import db as dbm
    dbm.enqueue(conn, pk, stage)
    dbm.mark(conn, pk, stage, status, error=error)


def row(conn, pk):
    r = conn.execute("SELECT * FROM reels WHERE pk = ?", (pk,)).fetchone()
    return dict(r)


# --------------------------------------------------------------------------- #
# Local stages.
# --------------------------------------------------------------------------- #

def run_stages(conn, pk, kind, photo_paths=None):
    """Run the local stages for one reel, reusing any stage already done."""
    import pipeline
    import db as dbm
    stages = pipeline.stages()
    ctx = pipeline.Context(conn)
    if kind == "photos":
        if queue_status(conn, pk, "describe_frames") not in ("done", "skipped"):
            import describe_frames as dfm
            descs = []
            for p in photo_paths or []:
                try:
                    d = dfm.vlm_describe(p)
                except Exception:
                    continue
                if d:
                    descs.append({"desc": d})
            blob = " ".join(s["desc"] for s in dfm.dedup_scenes(descs))
            dbm.set_visual(conn, pk, blob)
            if row(conn, pk).get("transcript") is None:
                dbm.set_transcript(conn, pk, "")
            for st in ("extract_audio", "transcribe"):
                if queue_status(conn, pk, st) is None:
                    set_queue(conn, pk, st, "skipped")
            set_queue(conn, pk, "sample_frames", "done")
            set_queue(conn, pk, "describe_frames", "done")
            log("  describe_frames(photos): %d photo(s) -> %d chars" % (len(photo_paths or []), len(blob)))
        todo = ("categorize", "tags")
    else:
        todo = STAGE_ORDER
    for name in todo:
        st = queue_status(conn, pk, name)
        if st in ("done", "skipped"):
            continue
        stage = stages[name]
        t0 = time.time()
        try:
            result = stage.process(row(conn, pk), ctx)
        except Exception as exc:
            set_queue(conn, pk, name, "failed", error=str(exc))
            raise RuntimeError("%s failed for %s: %s" % (name, pk, exc))
        stage.write(conn, pk, result)
        set_queue(conn, pk, name, "done")
        log("  %s: done in %.0fs" % (name, time.time() - t0))


def media_needed(conn, pk):
    return any(queue_status(conn, pk, s) not in ("done", "skipped")
               for s in ("categorize", "tags"))


def final_tags(conn, pk, index, counts):
    r = row(conn, pk)
    cats = json.loads(r.get("categories") or "[]")
    tags = json.loads(r.get("tags") or "[]")
    ids = tn.normalize(cats, tags, index, counts)
    return ids or ["other"]


def corpus_counts(conn):
    rows = conn.execute("SELECT tags FROM reels WHERE tags IS NOT NULL").fetchall()
    return tn.tag_counts(json.loads(r[0]) for r in rows)


# --------------------------------------------------------------------------- #
# Telegram.
# --------------------------------------------------------------------------- #

def bot_api(method, params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (token, method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:  # Telegram returns JSON bodies on 4xx
        return json.loads(e.read() or b"{}")


def edit_caption(message_id, caption):
    """editMessageCaption; returns the caption Telegram now has. Raises on error."""
    res = bot_api("editMessageCaption", {"chat_id": CHANNEL_ID, "message_id": message_id,
                                         "caption": caption})
    if not res.get("ok"):
        desc = res.get("description", "")
        if "message is not modified" in desc:
            return caption
        raise RuntimeError("editMessageCaption %s: %s" % (message_id, desc))
    return res["result"].get("caption", "")


def make_client(mode):
    from telethon import TelegramClient
    session = os.path.join(HERE, "tg_bot.session" if mode == "bot" else "tg_user.session")
    return TelegramClient(session, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
                          use_ipv6=os.environ.get("TG_USE_IPV6", "1") != "0")


async def start_client(client, mode):
    if mode == "bot":
        await client.start(bot_token=os.environ["TELEGRAM_BOT_TOKEN"])
    else:
        await client.start()  # interactive: phone + code, done by the owner once


async def scan_ids(client, start=1, chunk=100, empty_chunks=2):
    """Bot mode: all channel messages by id scan (bots can't getHistory)."""
    msgs = []
    empty = 0
    first = start
    while empty < empty_chunks:
        got = await client.get_messages(CHANNEL_ID, ids=list(range(first, first + chunk)))
        got = [m for m in got if m is not None]
        empty = 0 if got else empty + 1
        msgs.extend(got)
        first += chunk
    return msgs


async def find_nonparsed(client, mode, ids=None):
    if ids:
        got = await client.get_messages(CHANNEL_ID, ids=list(ids))
        return [m for m in got if m is not None and is_nonparsed(m.message)]
    if mode == "user":
        found = [m async for m in client.iter_messages(CHANNEL_ID, search=NONPARSED)]
    else:
        found = await scan_ids(client)
    found = [m for m in found if is_nonparsed(m.message)]
    return sorted(found, key=lambda m: m.id)


def kind_of(msg):
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "photo", None):
        return "photos"
    return "text"


async def album(client, msg):
    """All photo messages of msg's media group (msg included), in id order."""
    if not msg.grouped_id:
        return [msg]
    got = await client.get_messages(CHANNEL_ID, ids=list(range(msg.id - 9, msg.id + 10)))
    return sorted([m for m in got if m is not None and m.grouped_id == msg.grouped_id],
                  key=lambda m: m.id)


async def download(client, conn, msg, pk, kind):
    from storage import MEDIA_DIR, media_path
    if kind == "video":
        path = media_path(pk)
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            os.makedirs(MEDIA_DIR, exist_ok=True)
            await client.download_media(msg, file=path + ".part")
            os.replace(path + ".part", path)
            log("  downloaded %s (%.1f MB)" % (os.path.basename(path), os.path.getsize(path) / 1e6))
        if queue_status(conn, pk, "download") != "done":
            set_queue(conn, pk, "download", "done")
        if queue_status(conn, pk, "enrich") is None:
            set_queue(conn, pk, "enrich", "done")
        return [path]
    d = os.path.join(MEDIA_DIR, str(pk))
    os.makedirs(d, exist_ok=True)
    paths = []
    for i, m in enumerate(await album(client, msg), 1):
        p = os.path.join(d, "%02d.jpg" % i)
        if not os.path.exists(p):
            await client.download_media(m, file=p)
        paths.append(p)
    log("  downloaded %d photo(s) to %s" % (len(paths), d))
    return paths


# --------------------------------------------------------------------------- #
# Main loop.
# --------------------------------------------------------------------------- #

def backup_db(path):
    dst = "%s.bak-%s" % (path, datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, dst)
    log("db backup: %s" % dst)


async def process_post(client, conn, msg, index, counts, dry_run):
    kind = kind_of(msg)
    meta = parse_caption(msg.message)
    if kind == "text" or not meta["shortcode"]:
        log("#%d: skip (kind=%s, shortcode=%s)" % (msg.id, kind, meta["shortcode"]))
        return None
    pk = ensure_reel(conn, meta, msg.id, kind)
    log("#%d: %s pk=%s @%s" % (msg.id, kind, pk, meta["author"]))
    upsert_tg_post(conn, msg.id, pk=pk, kind=kind, author=meta["author"], date=meta["date"],
                   caption=msg.message)
    paths = []
    if media_needed(conn, pk):
        paths = await download(client, conn, msg, pk, kind)
    else:
        log("  categorize+tags already done, media not needed")
    run_stages(conn, pk, kind, paths)
    ids = final_tags(conn, pk, index, counts)
    new_caption = replace_last_line(msg.message, ids)
    log("  tags: %s" % tn.hashtag_line(ids))
    if dry_run:
        log("  dry-run: would set last line -> %r" % last_line(new_caption))
        return ids
    upsert_tg_post(conn, msg.id, tags=json.dumps(ids))
    got = edit_caption(msg.id, new_caption)
    if got.strip() != new_caption.strip():
        raise RuntimeError("#%d: caption mismatch after edit: %r" % (msg.id, last_line(got)))
    now = int(time.time())
    upsert_tg_post(conn, msg.id, caption=got, parsed_at=now)
    cols = reels_cols(conn)
    if "tags_final" in cols and "parsed_at" in cols:
        conn.execute("UPDATE reels SET tags_final = ?, parsed_at = ? WHERE pk = ?",
                     (json.dumps(ids), now, pk))
        conn.commit()
    log("  edited #%d, verified: %r" % (msg.id, last_line(got)))
    return ids


async def harvest(client, conn, mode):
    """--scan-all: record already-tagged posts into tg_posts (no edits)."""
    msgs = await scan_ids(client) if mode == "bot" else \
        [m async for m in client.iter_messages(CHANNEL_ID)]
    n = 0
    for m in msgs:
        last = last_line(m.message)
        if not last.startswith("#") or last in (NONPARSED, "#unavailable"):
            continue
        meta = parse_caption(m.message)
        ids = [w[1:] for w in last.split() if w.startswith("#")]
        existing = conn.execute("SELECT parsed_at FROM tg_posts WHERE message_id = ?",
                                (m.id,)).fetchone()
        if existing and existing[0]:
            continue
        pk = None
        if meta["shortcode"]:
            r = conn.execute("SELECT pk FROM reels WHERE shortcode = ?", (meta["shortcode"],)).fetchone()
            pk = r[0] if r else media_pk_from_code(meta["shortcode"])
        upsert_tg_post(conn, m.id, pk=pk, kind=kind_of(m), author=meta["author"],
                       date=meta["date"], caption=m.message, tags=json.dumps(ids),
                       parsed_at=int(m.edit_date.timestamp()) if m.edit_date else int(m.date.timestamp()))
        n += 1
    log("harvest: %d already-tagged post(s) recorded" % n)


async def amain(args):
    import db as dbm
    db_path = os.environ.get("REELS_DB", os.path.join(HERE, "reels.db"))
    conn = dbm.connect(db_path)
    if not args.only_index:
        if not args.no_backup:
            backup_db(db_path)
        dbm.init_db(conn)
        init_tg_posts(conn)
        tree = tn.load_tree(args.tree)
        index = tn.TreeIndex(tree)
        counts = corpus_counts(conn)
        client = make_client(args.mode)
        await start_client(client, args.mode)
        try:
            if args.scan_all:
                await harvest(client, conn, args.mode)
            ids = [int(x) for x in args.ids.split(",")] if args.ids else None
            todo = await find_nonparsed(client, args.mode, ids)
            log("found %d #nonparsed post(s)%s" % (
                len(todo), "" if args.limit is None else ", taking %d" % min(args.limit, len(todo))))
            if args.limit is not None:
                todo = todo[:args.limit]
            ok = fail = 0
            for i, msg in enumerate(todo, 1):
                t0 = time.time()
                try:
                    await process_post(client, conn, msg, index, counts, args.dry_run)
                    ok += 1
                except Exception as exc:
                    fail += 1
                    log("#%d: FAILED: %s" % (msg.id, exc))
                log("progress %d/%d (%.0fs)" % (i, len(todo), time.time() - t0))
            log("done: %d ok, %d failed" % (ok, fail))
        finally:
            await client.disconnect()
    else:
        init_tg_posts(conn)
    if args.dry_run and not args.only_index:
        return
    import index_build
    idx = index_build.build_index(conn, tn.load_tree(args.tree))
    path = index_build.write_index(idx, args.index_out)
    log("index: %d post(s) -> %s" % (len(idx["posts"]), path))
    if not args.no_publish:
        index_build.publish(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-index", action="store_true")
    ap.add_argument("--mode", choices=("bot", "user"), default="bot")
    ap.add_argument("--ids", help="comma-separated message ids to consider")
    ap.add_argument("--scan-all", action="store_true",
                    help="also record already-tagged posts into tg_posts")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--tree", default=tn.DEFAULT_TREE)
    ap.add_argument("--index-out", default=os.path.join(HERE, "index.json"))
    args = ap.parse_args(argv)
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
