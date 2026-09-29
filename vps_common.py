"""Shared IG -> Telegram posting code for the VPS side.

Used by vps_service.py (links sent to @libinstabot) and vps_download.py
(backlog batches run by the ig-download workflow). Stdlib only (instagrapi is
needed only by callers that create the Client), works on python 3.9 and 3.11.

Post kinds (SPEC 3.2):
  video   -> sendVideo with width/height/duration/thumbnail (first video slide of a carousel)
  photos  -> sendPhoto (1) / sendMediaGroup (2..10), caption on the first photo
  text    -> "⚠️ unavailable" + caption, last line #unavailable (IG returned no items)
Caption (SPEC 3.1): IG text (cut to fit) + link + "@author · date [· from X]" + tag line.
"""
import io, json, os, random, re, time, urllib.error, urllib.parse, urllib.request

NONPARSED = "#nonparsed"
UNAVAILABLE = "#unavailable"
CAPTION_LIMIT = 1024          # Telegram caption limit, counted in UTF-16 code units
TEXT_LIMIT = 4096
THUMB_MAX_SIDE = 320
THUMB_MAX_BYTES = 190_000     # Telegram: < 200 KB
VIDEO_MAX_BYTES = 49 * 1024 * 1024   # Bot API upload limit is 50 MB
MAX_ALBUM = 10

LINK_RE = re.compile(r"instagram\.com/(?!share/)(?:[A-Za-z0-9_.]+/)?(?:reels?|p|tv)/([A-Za-z0-9_-]+)")
SHARE_RE = re.compile(r"https?://(?:www\.)?instagram\.com/share/(?:reels?/|p/)?[A-Za-z0-9_-]+/?")
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

THROTTLE_MARKERS = ("429", "too many requests", "rate limit", "please wait", "feedback_required",
                    "pleasewaitfewminutes", "clientthrottled", "feedbackrequired")
LOGIN_MARKERS = ("login_required", "loginrequired", "challenge", "checkpoint", "reloginattempt")
NOT_FOUND_MARKERS = ("404", "media not found", "medianotfound", "clientnotfound", "not found")


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg, flush=True)


# ---------------------------------------------------------------- pure helpers

def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def cut_utf16(s, limit):
    """Longest prefix of s that fits into `limit` UTF-16 code units (never splits a surrogate pair)."""
    if limit <= 0:
        return ""
    if utf16_len(s) <= limit:
        return s
    out, n = [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > limit:
            break
        out.append(ch); n += w
    return "".join(out)


def parse_links(text):
    """Unique IG shortcodes from a message text/caption, in order of appearance."""
    return list(dict.fromkeys(LINK_RE.findall(text or "")))


def parse_share_links(text):
    """instagram.com/share/... links (they redirect to /reel/<code>/ and must be resolved over HTTP)."""
    return list(dict.fromkeys(SHARE_RE.findall(text or "")))


def pk_from_code(code):
    """Media pk from a shortcode; same result as instagrapi Client.media_pk_from_code (first 11 chars)."""
    pk = 0
    for ch in code[:11]:
        idx = CODE_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError("bad shortcode %r" % code)
        pk = pk * 64 + idx
    return str(pk)


def code_from_pk(pk):
    pk = int(str(pk).split("_")[0])
    out = ""
    while pk:
        pk, r = divmod(pk, 64)
        out = CODE_ALPHABET[r] + out
    return out or "A"


def caption_for(item, shared_by=None, tag=NONPARSED, fallback=None, prefix="", limit=CAPTION_LIMIT):
    """Caption by SPEC 3.1. `item` is an IG media dict (may be empty for unavailable posts);
    `fallback` = {"shortcode"|"code", "caption", "author", "taken_at"|"date"} fills gaps from the DM copy.
    The IG text is cut so the whole caption fits `limit` UTF-16 units; the tail is never cut."""
    fb = fallback or {}
    item = item or {}
    cap = ((item.get("caption") or {}).get("text") or fb.get("caption") or "").strip()
    user = (item.get("user") or {}).get("username") or fb.get("author") or ""
    code = item.get("code") or fb.get("shortcode") or fb.get("code") or ""
    if not code and (item.get("pk") or fb.get("pk")):
        code = code_from_pk(item.get("pk") or fb.get("pk"))
    taken = item.get("taken_at") or fb.get("taken_at")
    date = time.strftime("%Y-%m-%d", time.gmtime(int(taken))) if taken else (fb.get("date") or "")
    meta = " · ".join(x for x in ("@" + user if user else "", date) if x)
    if shared_by:
        meta = (meta + " · " if meta else "") + "from %s" % shared_by
    tail_lines = ["https://www.instagram.com/reel/%s/" % code]
    if meta:
        tail_lines.append(meta)
    if tag:
        tail_lines.append(tag)
    tail = "\n".join(tail_lines)
    head = prefix.strip()
    fixed = (head + "\n\n" if head else "") + tail
    room = limit - utf16_len(fixed) - 2          # 2 = "\n\n" between text and tail
    if cap and room > 0:
        if utf16_len(cap) > room:
            cap = cut_utf16(cap, room - 1).rstrip() + "…"
        body = (head + "\n\n" if head else "") + cap + "\n\n" + tail
    else:
        body = fixed
    return body


def pick_video(item):
    """(view, version) for the post's video, or (item, None). For a carousel the first video slide is
    used; `view` keeps parent caption/user/code but the slide's size/duration/thumbnails."""
    versions = item.get("video_versions") or []
    if versions:
        return item, versions[0]
    for child in item.get("carousel_media") or []:
        if child.get("video_versions"):
            view = dict(item, image_versions2=child.get("image_versions2"),
                        video_versions=child["video_versions"],
                        video_duration=child.get("video_duration"),
                        original_width=child.get("original_width"),
                        original_height=child.get("original_height"))
            return view, child["video_versions"][0]
    return item, None


def pick_thumb(item, max_side=THUMB_MAX_SIDE):
    """(url, needs_resize) for the video thumbnail, or (None, False).
    Prefers the largest IG candidate with both sides <= max_side (sent as is); IG clips often have
    none (240x426 is the smallest), then the smallest larger candidate is taken and must be
    downscaled by make_thumb()."""
    fit, big = None, None
    for c in (item.get("image_versions2") or {}).get("candidates") or []:
        w, h = c.get("width") or 0, c.get("height") or 0
        if not c.get("url") or not w or not h:
            continue
        if w <= max_side and h <= max_side:
            if fit is None or w * h > fit[0]:
                fit = (w * h, c["url"])
        elif big is None or w * h < big[0]:
            big = (w * h, c["url"])
    if fit:
        return fit[1], False
    if big:
        return big[1], True
    return None, False


def thumb_size(w, h, max_side=THUMB_MAX_SIDE):
    """Aspect-preserving size with the longer side = min(max_side, current)."""
    k = min(1.0, float(max_side) / max(w, h))
    return max(1, int(round(w * k))), max(1, int(round(h * k)))


def make_thumb(src, dst, max_side=THUMB_MAX_SIDE, max_bytes=THUMB_MAX_BYTES):
    """Downscale an image to a JPEG within max_side x max_side and < max_bytes. Needs Pillow
    (instagrapi pulls it in); returns False if Pillow is missing or the result is too big."""
    try:
        from PIL import Image
    except ImportError:
        return False
    with Image.open(src) as im:
        im = im.convert("RGB")
        im = im.resize(thumb_size(im.width, im.height, max_side))
        for q in (85, 70, 55, 40):
            im.save(dst, "JPEG", quality=q)
            if os.path.getsize(dst) < max_bytes:
                return True
    return False


def pick_photos(item, max_n=MAX_ALBUM):
    """URLs of the largest candidate of every photo (carousel slides or the post itself), <= max_n."""
    urls = []
    for child in (item.get("carousel_media") or [item]):
        cands = (child.get("image_versions2") or {}).get("candidates") or []
        if cands:
            best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
            urls.append(best["url"])
    return urls[:max_n]


def video_params(view, version):
    d = {"duration": int(round(float(view.get("video_duration") or 0)))}
    w = version.get("width") or view.get("original_width")
    h = version.get("height") or view.get("original_height")
    if w and h:
        d["width"], d["height"] = int(w), int(h)
    return d


def classify_error(text):
    """'login' (relogin needed), 'throttled' (back off), 'not_found' or None."""
    low = (text or "").lower()
    if any(m in low for m in LOGIN_MARKERS):
        return "login"
    if any(m in low for m in THROTTLE_MARKERS):
        return "throttled"
    if any(m in low for m in NOT_FOUND_MARKERS):
        return "not_found"
    return None


def channel_link(chat_id, message_id):
    s = str(chat_id)
    return "https://t.me/c/%s/%s" % (s[4:] if s.startswith("-100") else s.lstrip("-"), message_id)


# ---------------------------------------------------------------- network

def tg(method, data=None, files=None, timeout=300, token=None):
    """Bot API call as multipart/form-data. Never raises on HTTP errors: returns {"ok": False, ...}."""
    token = token or os.environ["TELEGRAM_BOT_TOKEN"]
    boundary = "----lib%d" % random.randint(1, 10**9)
    body = io.BytesIO()
    for k, v in (data or {}).items():
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
    for k, path in (files or {}).items():
        with open(path, "rb") as f:
            body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                        "Content-Type: application/octet-stream\r\n\r\n" % (boundary, k, os.path.basename(path))).encode())
            body.write(f.read()); body.write(b"\r\n")
    body.write(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (token, method), data=body.getvalue(),
                                 headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "description": "HTTP %s" % e.code}
    except Exception as e:  # network errors: keep the token out of the message
        return {"ok": False, "description": "%s: %s" % (type(e).__name__, str(e).replace(token, "***")[:200])}


def fetch(url, path, max_bytes=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    n = 0
    with urllib.request.urlopen(req, timeout=180) as resp, open(path, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            n += len(chunk)
            if max_bytes and n > max_bytes:
                raise ValueError("file larger than %d bytes" % max_bytes)
            f.write(chunk)
    return os.path.getsize(path)


def resolve_share_link(url):
    """Follow an instagram.com/share/... redirect and return the shortcode, or None."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        codes = parse_links(r.geturl())
    return codes[0] if codes else None


def _tg_error(res):
    return "tg: " + str(res.get("description") or res)[:300]


def _send_photos(chat, urls, cap, work, pk):
    """Album by CDN URLs; if Telegram can't fetch them, download and upload the files."""
    def by_url():
        if len(urls) == 1:
            return tg("sendPhoto", {"chat_id": chat, "photo": urls[0], "caption": cap})
        media = [{"type": "photo", "media": u} for u in urls]
        media[0]["caption"] = cap
        return tg("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)})

    res = by_url()
    if res.get("ok"):
        return res, "url"
    paths = []
    try:
        for i, u in enumerate(urls):
            p = os.path.join(work, "%s_%02d.jpg" % (pk, i))
            fetch(u, p); paths.append(p)
        if len(paths) == 1:
            return tg("sendPhoto", {"chat_id": chat, "caption": cap}, {"photo": paths[0]}), "upload"
        media = [{"type": "photo", "media": "attach://p%d" % i} for i in range(len(paths))]
        media[0]["caption"] = cap
        files = {"p%d" % i: p for i, p in enumerate(paths)}
        return tg("sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)}, files), "upload"
    finally:
        for p in paths:
            if os.path.exists(p):
                os.remove(p)


def post_media(cl, pk, shared_by=None, tag=NONPARSED, fallback=None, chat=None, work=None):
    """Fetch one IG media by pk and post it to the channel (SPEC 3.2). Nothing is kept on disk.

    Returns a result dict: status in sent | unavailable | failed | throttled (reason: throttled | login);
    kind in video | photos | text; message_id, file_id, width/height/duration, code, author,
    ig_caption, taken_at, error. `tag` is the caption's last line for video/photos; unavailable
    posts always get #unavailable."""
    pk = str(pk)
    chat = chat or os.environ["TG_CHAT_ID"]
    work = work or os.environ.get("WORK_DIR", "work")
    os.makedirs(work, exist_ok=True)
    rec = {"pk": pk, "status": "failed"}
    mp4 = os.path.join(work, pk + ".mp4"); jpg = os.path.join(work, pk + ".jpg")
    try:
        try:
            info = cl.private_request("media/%s/info/" % pk)
            items = info.get("items") or []
        except Exception as exc:
            if classify_error("%s %s" % (type(exc).__name__, exc)) != "not_found":
                raise
            items = []
        if not items:
            cap = caption_for({}, shared_by, UNAVAILABLE, dict(fallback or {}, pk=pk), prefix="⚠️ unavailable")
            res = tg("sendMessage", {"chat_id": chat, "text": cap, "disable_web_page_preview": "true"})
            if res.get("ok"):
                rec.update(status="unavailable", kind="text", message_id=res["result"]["message_id"])
            else:
                rec["error"] = _tg_error(res)
            return rec
        item = items[0]
        rec.update(media_type=item.get("media_type"), product_type=item.get("product_type"),
                   n_carousel=len(item.get("carousel_media") or []), code=item.get("code"),
                   author=(item.get("user") or {}).get("username"),
                   ig_caption=((item.get("caption") or {}).get("text") or "")[:2000],
                   taken_at=item.get("taken_at"))
        view, version = pick_video(item)
        cap = caption_for(view, shared_by, tag, fallback)
        if version is None:
            urls = pick_photos(item)
            if not urls:
                rec["error"] = "no video and no photos in media info"
                return rec
            res, how = _send_photos(chat, urls, cap, work, pk)
            if res.get("ok"):
                msgs = res["result"] if isinstance(res["result"], list) else [res["result"]]
                photo = (msgs[0].get("photo") or [{}])[-1]
                rec.update(status="sent", kind="photos", message_id=msgs[0]["message_id"],
                           message_ids=[m["message_id"] for m in msgs], n=len(msgs),
                           file_id=photo.get("file_id"), via=how)
            else:
                rec["error"] = _tg_error(res)
            return rec
        rec["carousel"] = view is not item
        rec["bytes"] = None
        err = None
        for v in (view.get("video_versions") or [version]):
            try:
                rec["bytes"] = fetch(v["url"], mp4, VIDEO_MAX_BYTES)
                version = v
                break
            except ValueError as exc:   # too large -> try a lower quality version
                err = str(exc)
        if rec["bytes"] is None:
            rec["error"] = "video: " + (err or "download failed")
            return rec
        data = dict(chat_id=chat, caption=cap, supports_streaming="true", **video_params(view, version))
        files = {"video": mp4}
        thumb_url, resize = pick_thumb(view)
        if thumb_url:
            try:
                if resize:
                    raw = jpg + ".src"
                    try:
                        fetch(thumb_url, raw)
                        ok = make_thumb(raw, jpg)
                    finally:
                        if os.path.exists(raw):
                            os.remove(raw)
                else:
                    ok = fetch(thumb_url, jpg) < THUMB_MAX_BYTES
                if ok:
                    files["thumbnail"] = jpg
            except Exception as exc:
                rec["thumb_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:100])
        res = tg("sendVideo", data, files)
        if res.get("ok"):
            m = res["result"]
            tv = m.get("video") or {}
            rec.update(status="sent", kind="video", message_id=m["message_id"],
                       file_id=tv.get("file_id"), thumb="thumbnail" in files,
                       width=data.get("width"), height=data.get("height"), duration=data["duration"],
                       # what Telegram stored — checked against width/height to catch a broken aspect ratio
                       tg_video={"width": tv.get("width"), "height": tv.get("height"),
                                 "duration": tv.get("duration"), "thumb": bool(tv.get("thumbnail") or tv.get("thumb"))})
        else:
            rec["error"] = _tg_error(res)
    except Exception as exc:
        msg = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        rec["error"] = msg
        kind = classify_error(msg)
        if kind in ("login", "throttled"):
            rec["status"] = "throttled"
            rec["reason"] = kind
    finally:
        for p in (mp4, jpg):
            if os.path.exists(p):
                os.remove(p)
    return rec
