"""VPS-side downloader: IG → Telegram channel, nothing kept on disk.

Runs on the tgbase self-hosted runner (Stockholm). For each pk: fetch media
info, download the mp4 and the IG thumbnail, sendVideo to the channel with
explicit width/height/duration (Telegram breaks the aspect ratio otherwise),
then delete both files immediately. No processing here — the VPS is too small;
transcribe/vision/tags run locally from the Telegram copy.

Env: IG_SESSION_JSON (base64 of ig_session.json), TELEGRAM_BOT_TOKEN,
TG_CHAT_ID, PKS (comma-separated), DELAY (seconds between IG calls, default 6),
TAG (optional last caption line, e.g. "#nonparsed"; empty = no tag line).
Output: results.jsonl — one {"pk","status","message_id","file_id",...} per pk.
"""
import base64, json, os, sys, time, random, urllib.request, urllib.parse

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TG_CHAT_ID"]
DELAY = float(os.environ.get("DELAY", "6"))
TAG = os.environ.get("TAG", "").strip()
WORK = "work"


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def tg(method, data=None, files=None):
    import http.client, mimetypes, io
    boundary = "----tgb%d" % random.randint(1, 10**9)
    body = io.BytesIO()
    for k, v in (data or {}).items():
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
    for k, path in (files or {}).items():
        with open(path, "rb") as f:
            body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n" % (boundary, k, os.path.basename(path))).encode())
            body.write(f.read()); body.write(b"\r\n")
    body.write(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (TOKEN, method), data=body.getvalue(),
                                 headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        return {"ok": False, "description": "HTTP %s %s" % (e.code, body)}


def fetch(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(path, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(path)


def caption_for(item, meta, tag=None):
    tag = TAG if tag is None else tag
    cap = ((item.get("caption") or {}).get("text") or "").strip() or (meta.get("caption") or "").strip()
    user = (item.get("user") or {}).get("username") or ""
    code = item.get("code") or meta.get("shortcode") or ""
    taken = item.get("taken_at")
    date = time.strftime("%Y-%m-%d", time.gmtime(taken)) if taken else ""
    shared_by = meta.get("shared_by") or ""
    tail = "https://www.instagram.com/reel/%s/\n@%s · %s" % (code, user, date)
    if shared_by:
        tail += " · from %s" % shared_by
    if tag:
        tail += "\n" + tag
    limit = 1024 - len(tail) - 2
    return (cap[:limit] + "\n\n" + tail).strip()


def main():
    from instagrapi import Client
    os.makedirs(WORK, exist_ok=True)
    with open("ig_session.json", "wb") as f:
        f.write(base64.b64decode(os.environ["IG_SESSION_JSON"]))
    cl = Client()
    cl.load_settings("ig_session.json")
    os.remove("ig_session.json")

    if os.environ.get("MODE") == "inbox":
        # read-only probe: can this session read DMs from here?
        r = cl.private_request("direct_v2/inbox/", params={"limit": "5", "persistentBadging": "true"})
        inbox = r.get("inbox", {})
        log("viewer=%s threads=%d unseen=%s" % ((r.get("viewer") or {}).get("username"), len(inbox.get("threads", [])), inbox.get("unseen_count")))
        for t in inbox.get("threads", []):
            items = t.get("items", [])
            last = max((it.get("timestamp", 0) for it in items), default=0) // 1_000_000
            shares = sum(1 for it in items if it.get("item_type", "").startswith(("xma_", "clip", "media_share")))
            log("  thread %s '%s' users=%d last=%s shares_in_page=%d" % (
                t.get("thread_id"), t.get("thread_title"), len(t.get("users", [])),
                time.strftime("%Y-%m-%d", time.gmtime(last)) if last else "?", shares))
        return

    pks = [p.strip() for p in os.environ.get("PKS", "").split(",") if p.strip()]
    metas = {}
    if os.path.exists("batch.json"):
        metas = {m["pk"]: m for m in json.load(open("batch.json"))}
        if not pks:
            pks = list(metas)
    log("batch of %d pk(s)" % len(pks))
    out = open("results.jsonl", "a")
    throttled = 0
    for i, pk in enumerate(pks, 1):
        rec = {"pk": pk, "status": "failed"}
        mp4 = os.path.join(WORK, pk + ".mp4"); jpg = os.path.join(WORK, pk + ".jpg")
        try:
            info = cl.private_request("media/%s/info/" % pk)
            items = info.get("items") or []
            item = items[0] if items else {}
            rec["media_type"] = item.get("media_type"); rec["product_type"] = item.get("product_type")
            rec["n_carousel"] = len(item.get("carousel_media") or [])
            versions = item.get("video_versions") or []
            if not versions:
                # carousel: take the first video child, keep the parent's caption/user
                for child in item.get("carousel_media") or []:
                    if child.get("video_versions"):
                        versions = child["video_versions"]
                        item = dict(item, image_versions2=child.get("image_versions2"),
                                    video_duration=child.get("video_duration"),
                                    original_width=child.get("original_width"),
                                    original_height=child.get("original_height"))
                        rec["carousel"] = True
                        break
            if not versions and not items:
                rec["status"] = "unavailable"
            if not versions:
                rec["status"] = "no_video"
            else:
                v = versions[0]
                size = fetch(v["url"], mp4)
                rec["bytes"] = size
                cands = [c for c in ((item.get("image_versions2") or {}).get("candidates") or [])
                         if (c.get("width") or 999) <= 320 and (c.get("height") or 999) <= 320]
                thumb = None
                if cands:
                    try:
                        if fetch(cands[0]["url"], jpg) < 190_000:
                            thumb = jpg
                    except Exception:
                        thumb = None
                data = {"chat_id": CHAT, "caption": caption_for(item, metas.get(pk, {})),
                        "supports_streaming": "true",
                        "duration": int(item.get("video_duration") or 0)}
                w = v.get("width") or item.get("original_width"); h = v.get("height") or item.get("original_height")
                if w and h:
                    data["width"], data["height"] = int(w), int(h)
                files = {"video": mp4}
                if thumb:
                    files["thumbnail"] = thumb
                res = tg("sendVideo", data, files)
                if res.get("ok"):
                    m = res["result"]
                    rec.update(status="sent", message_id=m["message_id"],
                               file_id=(m.get("video") or {}).get("file_id"),
                               width=data.get("width"), height=data.get("height"), duration=data["duration"],
                               code=item.get("code"), author=(item.get("user") or {}).get("username"),
                               ig_caption=((item.get("caption") or {}).get("text") or "")[:2000],
                               taken_at=item.get("taken_at"))
                else:
                    rec["error"] = "tg: " + str(res)[:300]
        except Exception as exc:
            msg = "%s: %s" % (type(exc).__name__, str(exc)[:300])
            rec["error"] = msg
            low = msg.lower()
            if any(s in low for s in ("429", "rate limit", "please wait", "feedback_required", "challenge", "login_required", "403")):
                throttled += 1
                rec["status"] = "throttled"
        finally:
            for p in (mp4, jpg):
                if os.path.exists(p):
                    os.remove(p)
        out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
        log("%d/%d %s %s %s" % (i, len(pks), pk, rec["status"], rec.get("error", "")))
        if rec["status"] == "throttled" and throttled >= 2:
            log("throttled twice — stopping this batch")
            break
        time.sleep(DELAY + random.uniform(0, DELAY / 2))
    out.close()
    leftover = os.listdir(WORK)
    if leftover:
        log("WARNING leftover files: %s" % leftover)
        for p in leftover:
            os.remove(os.path.join(WORK, p))
    log("done")


if __name__ == "__main__":
    main()
