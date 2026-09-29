"""libinsta downloader service (runs on the VPS, in docker).

Long-polls Telegram updates for the bot. Any Instagram link sent to the bot by
an allowed user is resolved through the IG cookie session, the video is
downloaded, posted to the channel with the IG caption + `#nonparsed`, and the
local file is deleted immediately. No processing happens here — tagging is
done on the Mac by parse_channel.py, which replaces `#nonparsed` in the caption.

Env: IG_SESSION_JSON (base64), TELEGRAM_BOT_TOKEN, TG_CHAT_ID, OWNER_IDS
(comma-separated telegram user ids allowed to feed links; empty = anyone,
logged), DATA_DIR (seen.json lives there), DELAY (seconds between IG calls).
"""
import base64, json, os, re, time, random, urllib.request, urllib.error, io

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TG_CHAT_ID"]
DATA = os.environ.get("DATA_DIR", "/data")
DELAY = float(os.environ.get("DELAY", "6"))
OWNERS = {s.strip() for s in os.environ.get("OWNER_IDS", "").split(",") if s.strip()}
WORK = os.path.join(DATA, "work")
SEEN = os.path.join(DATA, "seen.json")
LINK_RE = re.compile(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)")
NONPARSED = "#nonparsed"


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg, flush=True)


def tg(method, data=None, files=None, timeout=300):
    boundary = "----lib%d" % random.randint(1, 10**9)
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
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": "HTTP %s %s" % (e.code, e.read()[:300].decode("utf-8", "replace"))}


def fetch(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(path, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(path)


def caption_for(item, shared_by=None):
    cap = ((item.get("caption") or {}).get("text") or "").strip()
    user = (item.get("user") or {}).get("username") or ""
    code = item.get("code") or ""
    taken = item.get("taken_at")
    date = time.strftime("%Y-%m-%d", time.gmtime(taken)) if taken else ""
    tail = "https://www.instagram.com/reel/%s/\n@%s · %s" % (code, user, date)
    if shared_by:
        tail += " · from %s" % shared_by
    tail += "\n" + NONPARSED
    limit = 1024 - len(tail) - 2
    return (cap[:limit] + "\n\n" + tail).strip()


def load_seen():
    try:
        return json.load(open(SEEN))
    except Exception:
        return {}


def save_seen(seen):
    tmp = SEEN + ".tmp"
    json.dump(seen, open(tmp, "w"))
    os.replace(tmp, SEEN)


def post_media(cl, pk, shared_by=None):
    """Download one IG media by pk and post it to the channel. Returns a result dict."""
    rec = {"pk": pk, "status": "failed"}
    mp4 = os.path.join(WORK, pk + ".mp4"); jpg = os.path.join(WORK, pk + ".jpg")
    try:
        info = cl.private_request("media/%s/info/" % pk)
        items = info.get("items") or []
        item = items[0] if items else {}
        rec["product_type"] = item.get("product_type")
        versions = item.get("video_versions") or []
        if not versions:
            for child in item.get("carousel_media") or []:
                if child.get("video_versions"):
                    versions = child["video_versions"]
                    item = dict(item, image_versions2=child.get("image_versions2"),
                                video_duration=child.get("video_duration"),
                                original_width=child.get("original_width"),
                                original_height=child.get("original_height"))
                    break
        if not items:
            rec["status"] = "unavailable"; return rec
        if not versions:
            # photo post / photo carousel: album of up to 10 photos straight from the CDN
            photos = []
            for child in (item.get("carousel_media") or [item]):
                cands = ((child.get("image_versions2") or {}).get("candidates") or [])
                if cands:
                    photos.append(cands[0]["url"])
            if not photos:
                rec["status"] = "no_media"; return rec
            cap = caption_for(item, shared_by)
            media = [{"type": "photo", "media": u} for u in photos[:10]]
            media[0]["caption"] = cap
            res = tg("sendMediaGroup", {"chat_id": CHAT, "media": json.dumps(media)})
            if res.get("ok"):
                rec.update(status="sent", kind="photos", message_id=res["result"][0]["message_id"], n=len(media))
            else:
                rec["error"] = "tg: " + str(res.get("description"))[:300]
            return rec
        v = versions[0]
        rec["bytes"] = fetch(v["url"], mp4)
        thumb = None
        cands = [c for c in ((item.get("image_versions2") or {}).get("candidates") or [])
                 if (c.get("width") or 999) <= 320 and (c.get("height") or 999) <= 320]
        if cands:
            try:
                if fetch(cands[0]["url"], jpg) < 190_000:
                    thumb = jpg
            except Exception:
                thumb = None
        data = {"chat_id": CHAT, "caption": caption_for(item, shared_by), "supports_streaming": "true",
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
            rec.update(status="sent", kind="video", message_id=m["message_id"],
                       file_id=(m.get("video") or {}).get("file_id"), code=item.get("code"))
        else:
            rec["error"] = "tg: " + str(res.get("description"))[:300]
    except Exception as exc:
        msg = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        rec["error"] = msg
        low = msg.lower()
        if any(s in low for s in ("429", "rate limit", "please wait", "feedback_required", "challenge", "login_required")):
            rec["status"] = "throttled"
    finally:
        for p in (mp4, jpg):
            if os.path.exists(p):
                os.remove(p)
    return rec


def reply(chat_id, text, reply_to=None):
    d = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
    if reply_to:
        d["reply_to_message_id"] = reply_to
    tg("sendMessage", d, timeout=30)


def handle_message(cl, msg, seen):
    text = (msg.get("text") or msg.get("caption") or "")
    uid = str((msg.get("from") or {}).get("id"))
    chat_id = msg["chat"]["id"]
    if OWNERS and uid not in OWNERS:
        log("ignored message from %s" % uid)
        return
    if text.startswith("/start") or text.startswith("/help"):
        reply(chat_id, "Кидай ссылки на Instagram (reel / p / tv) — скачаю и запощу в канал с #nonparsed.\n/status — статистика.")
        return
    if text.startswith("/status"):
        reply(chat_id, "seen: %d\nowner: %s" % (len(seen), uid))
        return
    codes = LINK_RE.findall(text)
    if not codes:
        reply(chat_id, "Не вижу ссылки на Instagram.", msg.get("message_id"))
        return
    from instagrapi.extractors import extract_media_v1  # noqa: F401  (import check)
    for code in dict.fromkeys(codes):
        try:
            pk = str(cl.media_pk_from_code(code))
        except Exception as exc:
            reply(chat_id, "%s: не смогла разобрать код (%s)" % (code, exc), msg.get("message_id"))
            continue
        if pk in seen:
            reply(chat_id, "%s уже в канале (msg %s)" % (code, seen[pk].get("message_id")), msg.get("message_id"))
            continue
        rec = post_media(cl, pk)
        log("link %s -> %s %s" % (code, rec["status"], rec.get("error", "")))
        if rec["status"] == "sent":
            seen[pk] = {"message_id": rec["message_id"], "kind": rec.get("kind"), "ts": int(time.time())}
            save_seen(seen)
            reply(chat_id, "✅ %s → msg %s" % (code, rec["message_id"]), msg.get("message_id"))
        else:
            reply(chat_id, "❌ %s: %s %s" % (code, rec["status"], rec.get("error", "")), msg.get("message_id"))
        time.sleep(DELAY + random.uniform(0, DELAY / 2))


def main():
    from instagrapi import Client
    os.makedirs(WORK, exist_ok=True)
    sess = os.path.join(DATA, "ig_session.json")
    with open(sess, "wb") as f:
        f.write(base64.b64decode(os.environ["IG_SESSION_JSON"]))
    cl = Client()
    cl.load_settings(sess)
    os.remove(sess)
    seen = load_seen()
    log("service up; seen=%d owners=%s" % (len(seen), sorted(OWNERS) or "any"))
    offset = 0
    while True:
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": json.dumps(["message"])}, timeout=70)
            if not res.get("ok"):
                log("getUpdates: %s" % res.get("description")); time.sleep(5); continue
            for upd in res["result"]:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if msg and msg["chat"]["type"] == "private":
                    handle_message(cl, msg, seen)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log("loop error: %s: %s" % (type(exc).__name__, str(exc)[:300]))
            time.sleep(5)


if __name__ == "__main__":
    main()
