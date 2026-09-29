"""VPS-side downloader: IG → Telegram channel, nothing kept on disk.

Runs on the tgbase self-hosted runner (Stockholm). For each pk: fetch media
info, download the mp4 and the IG thumbnail, sendVideo to the channel with
explicit width/height/duration (Telegram breaks the aspect ratio otherwise),
then delete both files immediately. No processing here — the VPS is too small;
transcribe/vision/tags run locally from the Telegram copy.

Env: IG_SESSION_JSON (base64 of ig_session.json), TELEGRAM_BOT_TOKEN,
TG_CHAT_ID, PKS (comma-separated), DELAY (seconds between IG calls, default 6).
Output: results.jsonl — one {"pk","status","message_id","file_id",...} per pk.
"""
import base64, json, os, sys, time, random, urllib.request, urllib.parse

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TG_CHAT_ID"]
DELAY = float(os.environ.get("DELAY", "6"))
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
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def fetch(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(path, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(path)


def caption_for(item, meta):
    cap = ((item.get("caption") or {}).get("text") or "").strip()
    user = (item.get("user") or {}).get("username") or ""
    code = item.get("code") or meta.get("shortcode") or ""
    taken = item.get("taken_at")
    date = time.strftime("%Y-%m-%d", time.gmtime(taken)) if taken else ""
    shared_by = meta.get("shared_by") or ""
    tail = "https://www.instagram.com/reel/%s/\n@%s · %s" % (code, user, date)
    if shared_by:
        tail += " · from %s" % shared_by
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
            versions = item.get("video_versions") or []
            if not versions:
                rec["status"] = "no_video"
            else:
                v = versions[0]
                size = fetch(v["url"], mp4)
                rec["bytes"] = size
                cands = ((item.get("image_versions2") or {}).get("candidates") or [])
                thumb = None
                if cands:
                    try:
                        fetch(cands[-1]["url"], jpg); thumb = jpg
                    except Exception:
                        thumb = None
                data = {"chat_id": CHAT, "caption": caption_for(item, metas.get(pk, {})),
                        "supports_streaming": "true",
                        "width": v.get("width") or item.get("original_width") or "",
                        "height": v.get("height") or item.get("original_height") or "",
                        "duration": int(item.get("video_duration") or 0)}
                files = {"video": mp4}
                if thumb:
                    files["thumbnail"] = thumb
                res = tg("sendVideo", data, files)
                if res.get("ok"):
                    m = res["result"]
                    rec.update(status="sent", message_id=m["message_id"],
                               file_id=(m.get("video") or {}).get("file_id"),
                               width=data["width"], height=data["height"], duration=data["duration"],
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
