"""libinsta downloader service (runs on the VPS, in docker, restart unless-stopped).

Long-polls Telegram updates for @libinstabot. Every Instagram link (reel / p / tv /
share) in a private message from an allowed user is resolved to a media pk, posted
to the channel by vps_common.post_media (video / photos / unavailable, SPEC 3.2) with
`#nonparsed`, and the user gets "✅ msg <id>" or "❌ <reason>". Tagging happens on the
Mac (parse_channel.py replaces `#nonparsed`).

IG throttling / challenge / login_required -> the service pauses IG for 15 min, tells
the user, keeps the link in /data/queue.json and retries it after the pause. It never
exits on errors; everything is logged to stdout (`docker logs libinsta`).

Env: IG_SESSION_JSON (base64), TELEGRAM_BOT_TOKEN, TG_CHAT_ID, OWNER_IDS
(comma-separated telegram user ids; empty = anyone, ids are logged), DATA_DIR
(seen.json, queue.json), DELAY (seconds between IG calls), PAUSE (seconds, default 900).

Search (iteration 4, bot_search.py) — owners only, and only when OWNER_IDS is set:
/tags (inline tree -> posts by 5 via copyMessage from the channel), /find <words>,
/random [tag], /reload. Index: GH_INDEX_TOKEN + GH_INDEX_REPO -> GitHub contents API every
10 min, cached to /data/index.json; without a token the file /data/index.json itself (the deploy
workflow puts it there), re-read when its mtime changes.
"""
import base64, json, os, random, time

import bot_search as bs

from vps_common import (NONPARSED, channel_link, log, parse_links, parse_share_links,
                        pk_from_code, post_media, resolve_share_link, tg)

DATA = os.environ.get("DATA_DIR", "/data")
DELAY = float(os.environ.get("DELAY", "6"))
PAUSE = int(os.environ.get("PAUSE", "900"))
WORK = os.path.join(DATA, "work")
SEEN = os.path.join(DATA, "seen.json")
QUEUE = os.path.join(DATA, "queue.json")
INDEX = os.path.join(DATA, "index.json")

HELP = ("Кидай ссылки на Instagram (reel / p / tv / share) — скачаю и запощу в канал libinsta "
        "с #nonparsed. Можно несколько ссылок в одном сообщении или пересланный пост с ссылкой в подписи.\n"
        "/status — состояние сервиса\n/help — эта подсказка\n\n"
        "Поиск по каналу:\n/tags — дерево тегов\n/find <слова> — поиск по тексту и тегам\n"
        "/random [тег] — случайный пост\n/reload — перечитать индекс")
SEARCH_CMDS = ("tags", "find", "random", "reload")


def parse_owner_ids(s):
    return {x.strip() for x in (s or "").split(",") if x.strip()}


def is_allowed(uid, owners):
    return not owners or str(uid) in owners


def command_of(text):
    """'/status@libinstabot arg' -> 'status'; None if not a command."""
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    return t[1:].split()[0].split("@")[0].lower() if len(t) > 1 else None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def command_arg(text):
    """'/find@libinstabot паста  карбонара' -> 'паста  карбонара'."""
    parts = (text or "").strip().split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


class Service:
    def __init__(self, cl, chat, owners, store=None):
        self.cl, self.chat, self.owners = cl, chat, owners
        self.store = store or bs.IndexStore(INDEX, os.environ.get("GH_INDEX_REPO", "padington/libinsta-index"),
                                            os.environ.get("GH_INDEX_TOKEN"), logf=log)
        self.finds = {}          # chat_id -> (query, [post indexes]) of the last /find
        self.seen = load_json(SEEN, {})
        self.queue = load_json(QUEUE, [])      # [{"code","pk","chat_id","reply_to","uid"}]
        self.paused_until = 0
        self.started = time.time()
        self.stats = {"sent": 0, "unavailable": 0, "failed": 0, "throttled": 0}
        self.last_error = ""

    # -- telegram replies
    def reply(self, chat_id, text, reply_to=None):
        d = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
        if reply_to:
            d["reply_to_message_id"] = reply_to
            d["allow_sending_without_reply"] = "true"
        res = tg("sendMessage", d, timeout=30)
        if not res.get("ok"):
            log("reply to %s failed: %s" % (chat_id, res.get("description")))

    def status_text(self, uid):
        up = int(time.time() - self.started)
        paused = ("IG на паузе до %s UTC" % time.strftime("%H:%M", time.gmtime(self.paused_until))
                  if self.paused_until > time.time() else "IG: ok")
        return ("в канале (seen): %d\nв очереди: %d\n%s\nза этот запуск: %s\naptime: %dч %dм\n"
                "твой id: %s\nOWNER_IDS: %s%s" % (
                    len(self.seen), len(self.queue), paused,
                    ", ".join("%s %d" % kv for kv in self.stats.items()), up // 3600, up % 3600 // 60,
                    uid, ",".join(sorted(self.owners)) or "не задан (принимаю от всех)",
                    ("\nпоследняя ошибка: " + self.last_error) if self.last_error else "")) + (
                "\nиндекс: %d постов (%s)%s" % (len(self.store.idx.posts), self.store.source or "—",
                                                ", ошибка: " + self.store.error if self.store.error else ""))

    # -- incoming messages
    def handle_message(self, msg):
        text = msg.get("text") or msg.get("caption") or ""
        frm = msg.get("from") or {}
        uid = str(frm.get("id"))
        chat_id = msg["chat"]["id"]
        mid = msg.get("message_id")
        log("msg from id=%s @%s: %r" % (uid, frm.get("username"), text[:120]))
        if not is_allowed(uid, self.owners):
            log("ignored: %s not in OWNER_IDS" % uid)
            return
        cmd = command_of(text)
        if cmd in ("start", "help"):
            self.reply(chat_id, HELP); return
        if cmd == "status":
            self.reply(chat_id, self.status_text(uid)); return
        if cmd in SEARCH_CMDS:
            self.handle_search(cmd, command_arg(text), chat_id, uid, mid); return
        codes = parse_links(text)
        for url in parse_share_links(text):
            try:
                code = resolve_share_link(url)
            except Exception as exc:
                code = None
                log("share link %s: %s" % (url, exc))
            if code:
                codes.append(code)
            else:
                self.reply(chat_id, "❌ %s: не смогла раскрыть share-ссылку" % url, mid)
        codes = list(dict.fromkeys(codes))
        if not codes:
            if not parse_share_links(text):
                self.reply(chat_id, "Не вижу ссылки на Instagram. /help", mid)
            return
        for code in codes:
            try:
                pk = pk_from_code(code)
            except ValueError as exc:
                self.reply(chat_id, "❌ %s: %s" % (code, exc), mid); continue
            if pk in self.seen:
                m = self.seen[pk].get("message_id")
                self.reply(chat_id, "%s уже в канале → msg %s %s" % (code, m, channel_link(self.chat, m)), mid)
                continue
            if any(q["pk"] == pk for q in self.queue):
                self.reply(chat_id, "%s уже в очереди" % code, mid); continue
            self.queue.append({"code": code, "pk": pk, "chat_id": chat_id, "reply_to": mid, "uid": uid})
        save_json(QUEUE, self.queue)
        if self.paused_until > time.time() and self.queue:
            self.reply(chat_id, "⏸ IG на паузе до %s UTC — ссылки в очереди (%d), выложу после паузы." % (
                time.strftime("%H:%M", time.gmtime(self.paused_until)), len(self.queue)), mid)

    # -- search (iteration 4)
    def search_allowed(self, uid):
        return bool(self.owners) and str(uid) in self.owners

    def send(self, chat_id, text, kb=None, reply_to=None):
        d = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
        if kb:
            d["reply_markup"] = json.dumps({"inline_keyboard": kb}, ensure_ascii=False)
        if reply_to:
            d["reply_to_message_id"] = reply_to
            d["allow_sending_without_reply"] = "true"
        res = tg("sendMessage", d, timeout=30)
        if not res.get("ok"):
            log("sendMessage to %s failed: %s" % (chat_id, res.get("description")))
        return res

    def edit(self, chat_id, message_id, text, kb=None):
        d = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
             "disable_web_page_preview": "true",
             "reply_markup": json.dumps({"inline_keyboard": kb or []}, ensure_ascii=False)}
        res = tg("editMessageText", d, timeout=30)
        if not res.get("ok") and "not modified" not in (res.get("description") or ""):
            log("editMessageText failed: %s" % res.get("description"))
            return False
        return True

    def copy_posts(self, chat_id, message_ids):
        """copyMessage each channel post to the user; returns the ids that failed."""
        failed = []
        for m in message_ids:
            res = tg("copyMessage", {"chat_id": chat_id, "from_chat_id": self.chat, "message_id": m}, timeout=60)
            if not res.get("ok"):
                failed.append(m)
                log("copyMessage %s -> %s failed: %s" % (m, chat_id, res.get("description")))
        return failed

    def show_posts(self, chat_id, ids, page, kind, key, label):
        text, kb, mids = bs.posts_nav(self.store.idx, ids, page, kind, key, label)
        failed = self.copy_posts(chat_id, mids)
        if failed:
            text += "\n⚠️ не скопировались: %s" % ", ".join(map(str, failed))
        self.send(chat_id, text, kb)

    def show_random(self, chat_id, tag):
        idx = self.store.idx
        i = idx.random_post(tag)
        if i is None:
            self.send(chat_id, "Нет постов%s." % (" с тегом #%s" % tag if tag else "")); return
        p = idx.posts[i]
        failed = self.copy_posts(chat_id, [p["m"]])
        text = "🎲 %s · msg %s%s" % ("#" + tag if tag else "любой тег", p["m"],
                                    " — ⚠️ не скопировался" if failed else "")
        self.send(chat_id, text, [[bs.btn("🎲 ещё", "r:" + (tag or ""))]])

    def handle_search(self, cmd, arg, chat_id, uid, mid=None):
        if not self.search_allowed(uid):
            log("search ignored: %s not in OWNER_IDS (%s)" % (uid, ",".join(sorted(self.owners)) or "empty"))
            self.reply(chat_id, "Поиск доступен только владельцу (OWNER_IDS).", mid); return
        idx = self.store.idx
        if cmd == "reload":
            ok, msg = self.store.reload()
            self.reply(chat_id, ("✅ " if ok else "❌ ") + msg, mid); return
        if not idx.posts and self.store.error:
            self.reply(chat_id, "❌ индекс не загружен: %s\n/reload — попробовать ещё раз" % self.store.error, mid)
            return
        if cmd == "tags":
            text, kb = bs.tree_view(idx, None)
            self.send(chat_id, text, kb); return
        if cmd == "find":
            if not arg:
                self.reply(chat_id, "Напиши слова: /find паста карбонара", mid); return
            ids = idx.find(arg)
            self.finds[chat_id] = (arg, ids)
            self.show_posts(chat_id, ids, 0, "f", None, "🔎 «%s»" % arg[:60]); return
        if cmd == "random":
            tag = None
            if arg:
                tag = idx.resolve_tag(arg)
                if not tag:
                    self.reply(chat_id, "Не знаю тег «%s». /tags — дерево тегов" % arg, mid); return
            self.show_random(chat_id, tag)

    def handle_callback(self, cq):
        uid = str((cq.get("from") or {}).get("id"))
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        data = cq.get("data") or ""
        log("callback from id=%s: %r" % (uid, data))
        allowed = self.search_allowed(uid)
        tg("answerCallbackQuery", {"callback_query_id": cq["id"],
                                   **({} if allowed else {"text": "Только для владельца", "show_alert": "true"})},
           timeout=15)
        if not allowed or chat_id is None:
            return
        idx = self.store.idx
        kind, key, page = bs.parse_callback(data)
        if kind == "n":
            if key is not None and key not in idx.nodes:
                key = None
            text, kb = bs.tree_view(idx, key)
            self.edit(chat_id, msg.get("message_id"), text, kb)
        elif kind == "p":
            self.edit(chat_id, msg.get("message_id"), msg.get("text") or "…", [])   # drop old buttons
            label = "🏷 " + " › ".join(idx.title(t) for t in idx.path(key)) if key in idx.nodes else "#" + key
            self.show_posts(chat_id, idx.post_ids(key), page, "p", key, label)
        elif kind == "f":
            q, ids = self.finds.get(chat_id, ("", None))
            if ids is None:
                self.send(chat_id, "Результат поиска устарел (сервис перезапускался). Повтори /find."); return
            self.edit(chat_id, msg.get("message_id"), msg.get("text") or "…", [])
            self.show_posts(chat_id, ids, page, "f", None, "🔎 «%s»" % q[:60])
        elif kind == "r":
            self.show_random(chat_id, key if key in idx.nodes or key in idx.by_tag else None)

    # -- queue worker (one IG call per step)
    def process_one(self):
        if not self.queue or self.paused_until > time.time():
            return False
        job = self.queue[0]
        rec = post_media(self.cl, job["pk"], tag=NONPARSED, fallback={"shortcode": job["code"]},
                         chat=self.chat, work=WORK)
        st = rec["status"]
        self.stats[st if st in self.stats else "failed"] += 1
        log("link %s pk=%s -> %s %s %s" % (job["code"], job["pk"], st, rec.get("kind", ""), rec.get("error", "")))
        if st == "throttled":
            self.paused_until = time.time() + PAUSE
            self.last_error = rec.get("error", "")[:200]
            job["tries"] = job.get("tries", 0) + 1
            save_json(QUEUE, self.queue)
            if job["tries"] > 1:      # tell the user once per link, not every 15 minutes
                return True
            why = ("IG просит перелогин (login_required/challenge) — нужен новый IG_SESSION_JSON."
                   if rec.get("reason") == "login" else "IG притормозил (throttle).")
            self.reply(job["chat_id"], "⏸ %s %s Пауза %d мин, ссылка останется в очереди и уйдёт после паузы.\n%s" % (
                job["code"], why, PAUSE // 60, rec.get("error", "")[:200]), job.get("reply_to"))
            return True     # keep the job at the head of the queue
        self.queue.pop(0)
        save_json(QUEUE, self.queue)
        if st in ("sent", "unavailable"):
            self.seen[job["pk"]] = {"message_id": rec["message_id"], "kind": rec.get("kind"),
                                    "code": job["code"], "ts": int(time.time())}
            save_json(SEEN, self.seen)
            what = {"video": "видео", "photos": "фото ×%s" % rec.get("n", 1), "text": "⚠️ недоступен в IG"}.get(rec.get("kind"), "")
            self.reply(job["chat_id"], "✅ %s → msg %s (%s)\n%s" % (
                job["code"], rec["message_id"], what, channel_link(self.chat, rec["message_id"])), job.get("reply_to"))
        else:
            self.last_error = rec.get("error", "")[:200]
            self.reply(job["chat_id"], "❌ %s: %s" % (job["code"], rec.get("error", st)), job.get("reply_to"))
        time.sleep(DELAY + random.uniform(0, DELAY / 2))
        return True

    def run(self):
        log("service up; seen=%d queue=%d owners=%s" % (len(self.seen), len(self.queue),
                                                        ",".join(sorted(self.owners)) or "any"))
        self.store.reload()
        offset = 0
        while True:
            try:
                busy = bool(self.queue) and self.paused_until <= time.time()
                res = tg("getUpdates", {"offset": offset, "timeout": 0 if busy else 50,
                                        "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=70)
                if not res.get("ok"):
                    log("getUpdates: %s" % res.get("description")); time.sleep(5); continue
                for upd in res["result"]:
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if msg and msg.get("chat", {}).get("type") == "private":
                        self.handle_message(msg)
                    elif upd.get("callback_query"):
                        try:
                            self.handle_callback(upd["callback_query"])
                        except Exception as exc:
                            log("callback error: %s: %s" % (type(exc).__name__, str(exc)[:300]))
                self.store.maybe_reload()
                self.process_one()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                log("loop error: %s: %s" % (type(exc).__name__, str(exc)[:300]))
                time.sleep(5)


def main():
    from instagrapi import Client
    os.makedirs(WORK, exist_ok=True)
    sess = os.path.join(DATA, "ig_session.json")
    with open(sess, "wb") as f:
        f.write(base64.b64decode(os.environ["IG_SESSION_JSON"]))
    cl = Client()
    try:
        cl.load_settings(sess)
    finally:
        os.remove(sess)
    Service(cl, os.environ["TG_CHAT_ID"], parse_owner_ids(os.environ.get("OWNER_IDS"))).run()


if __name__ == "__main__":
    main()
