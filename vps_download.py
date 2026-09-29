"""VPS-side backlog downloader: IG -> Telegram channel, nothing kept on disk.

Runs in the ig-download workflow on the tgbase self-hosted runner (Stockholm), inside
python:3.11-slim. For each pk (in the given order — backlog.py sends newest first) it
calls vps_common.post_media — the same code the bot service uses: video / photos /
unavailable per SPEC 3.2, caption per 3.1 with `#nonparsed` (`#unavailable` for gone posts).

Env: IG_SESSION_JSON (base64 of ig_session.json), TELEGRAM_BOT_TOKEN, TG_CHAT_ID,
PKS (comma-separated; optional if batch.json exists), DELAY (seconds between IG calls,
default 6), MODE=download|inbox,
TAG (last caption line, default #nonparsed; set but empty = no tag line).
batch.json (optional): [{"pk", "shortcode", "shared_by", "caption"}, ...] — shared_by goes to
the caption, shortcode/caption are fallbacks for unavailable posts.
Output: results.jsonl — one post_media result per pk:
  {"pk","status": sent|unavailable|failed|throttled, "kind": video|photos|text, "message_id",
   "file_id","width","height","duration","code","author","ig_caption","taken_at","error",...}
Two throttled results stop the batch.
"""
import base64, json, os, random, sys, time

sys.dont_write_bytecode = True   # runs as root in docker on a bind mount: no root-owned __pycache__ for the runner
from vps_common import NONPARSED, log, post_media

WORK = "work"


def load_batch(path="batch.json", pks_env=""):
    """(pks, metas): PKS env wins for the order/selection; metas come from batch.json."""
    metas = {}
    if os.path.exists(path):
        with open(path) as f:
            metas = {str(m["pk"]): m for m in json.load(f)}
    pks = [p.strip() for p in (pks_env or "").split(",") if p.strip()] or list(metas)
    return pks, metas


def inbox_probe(cl):
    """Read-only probe: can this session read DMs from here?"""
    r = cl.private_request("direct_v2/inbox/", params={"limit": "5", "persistentBadging": "true"})
    inbox = r.get("inbox", {})
    log("viewer=%s threads=%d unseen=%s" % ((r.get("viewer") or {}).get("username"),
                                             len(inbox.get("threads", [])), inbox.get("unseen_count")))
    for t in inbox.get("threads", []):
        items = t.get("items", [])
        last = max((it.get("timestamp", 0) for it in items), default=0) // 1_000_000
        shares = sum(1 for it in items if it.get("item_type", "").startswith(("xma_", "clip", "media_share")))
        log("  thread %s '%s' users=%d last=%s shares_in_page=%d" % (
            t.get("thread_id"), t.get("thread_title"), len(t.get("users", [])),
            time.strftime("%Y-%m-%d", time.gmtime(last)) if last else "?", shares))


def main():
    from instagrapi import Client
    delay = float(os.environ.get("DELAY", "6"))
    chat = os.environ["TG_CHAT_ID"]
    tag = os.environ.get("TAG", NONPARSED).strip()
    os.makedirs(WORK, exist_ok=True)
    with open("ig_session.json", "wb") as f:
        f.write(base64.b64decode(os.environ["IG_SESSION_JSON"]))
    cl = Client()
    try:
        cl.load_settings("ig_session.json")
    finally:
        os.remove("ig_session.json")

    if os.environ.get("MODE") == "inbox":
        inbox_probe(cl)
        return

    pks, metas = load_batch(pks_env=os.environ.get("PKS", ""))
    log("batch of %d pk(s)" % len(pks))
    throttled = 0
    counts = {}
    with open("results.jsonl", "a") as out:
        for i, pk in enumerate(pks, 1):
            meta = metas.get(pk, {})
            rec = post_media(cl, pk, shared_by=meta.get("shared_by") or None, tag=tag,
                             fallback=meta, chat=chat, work=WORK)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            log("%d/%d %s %s %s msg=%s %s" % (i, len(pks), pk, rec["status"], rec.get("kind", ""),
                                            rec.get("message_id", "-"), rec.get("error", "")))
            if rec["status"] == "throttled":
                throttled += 1
                if throttled >= 2 or rec.get("reason") == "login":
                    log("throttled (%s) — stopping this batch" % rec.get("reason"))
                    break
            if i < len(pks):
                time.sleep(delay + random.uniform(0, delay / 2))
    leftover = os.listdir(WORK)
    if leftover:
        log("WARNING leftover files: %s" % leftover)
        for p in leftover:
            os.remove(os.path.join(WORK, p))
    log("done: %s" % json.dumps(counts))


if __name__ == "__main__":
    main()
