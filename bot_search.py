"""Search over index.json for @libinstabot (SPEC §3.5, iteration 4).

Pure functions + a small Index class; no network here except `fetch_index_github`.
The service (vps_service.py) wires them to Telegram: /tags, /find, /random, /reload
and inline-keyboard callbacks.

index.json: {"version": 1, "built_at": ..., "tree": [{id, title, children: [...]}, ...],
             "posts": [{"m": msg_id, "k": kind, "t": [tags], "d": date, "a": author, "s": text}]}

Callback data (<= 64 bytes, Telegram limit):
    n:            tree root
    n:<id>        open node <id> (children with counts, or its posts if it is a leaf)
    p:<id>:<pg>   posts of node/tag <id>, page <pg>
    f:<pg>        page <pg> of the chat's last /find result (kept in memory)
    r:<id>        one more random post (id may be empty = any post)
"""
import json
import os
import random
import re
import time
import urllib.request

PAGE = 5
MAX_DEPTH = 3
WORD_RE = re.compile(r"[\w#]+", re.UNICODE)


def norm(s):
    return (s or "").lower().replace("ё", "е")


class Index:
    def __init__(self, data):
        data = data or {}
        self.built_at = data.get("built_at") or 0
        self.tree = data.get("tree") or []
        # newest first: bigger message_id = posted later
        self.posts = sorted(data.get("posts") or [], key=lambda p: -int(p.get("m") or 0))
        self.nodes, self.parent, self.depth = {}, {}, {}
        self._walk(self.tree, None, 1)
        self.by_tag = {}
        for i, p in enumerate(self.posts):
            for t in p.get("t") or []:
                self.by_tag.setdefault(t, []).append(i)
        self._sub = {}

    def _walk(self, nodes, parent, depth):
        for n in nodes or []:
            nid = n.get("id")
            if not nid or nid in self.nodes:
                continue
            self.nodes[nid], self.parent[nid], self.depth[nid] = n, parent, depth
            if depth < MAX_DEPTH:
                self._walk(n.get("children"), nid, depth + 1)

    # -- tree
    def children(self, nid=None):
        kids = self.tree if nid is None else (self.nodes.get(nid) or {}).get("children") or []
        if nid is not None and self.depth.get(nid, MAX_DEPTH) >= MAX_DEPTH:
            return []
        return [k for k in kids if k.get("id") in self.nodes and self.parent[k["id"]] == nid]

    def subtree_ids(self, nid):
        out, stack = [], [nid]
        while stack:
            x = stack.pop()
            out.append(x)
            stack.extend(k["id"] for k in self.children(x))
        return out

    def post_ids(self, tag):
        """Indexes (into self.posts, newest first) of posts tagged with tag or any descendant."""
        if tag not in self._sub:
            ids = set()
            for t in (self.subtree_ids(tag) if tag in self.nodes else [tag]):
                ids.update(self.by_tag.get(t, ()))
            self._sub[tag] = sorted(ids)
        return self._sub[tag]

    def count(self, tag):
        return len(self.post_ids(tag))

    def title(self, tag):
        n = self.nodes.get(tag)
        return (n.get("title") or tag) if n else tag

    def path(self, tag):
        out = []
        while tag:
            out.append(tag)
            tag = self.parent.get(tag)
        return out[::-1]

    def resolve_tag(self, word):
        """'#Pasta' / 'pasta' / 'Паста' / alias -> tag id or None."""
        w = norm(word).lstrip("#").strip()
        if not w:
            return None
        if w in self.nodes or w in self.by_tag:
            return w
        for nid, n in self.nodes.items():
            if norm(n.get("title")) == w:
                return nid
        for nid, n in self.nodes.items():
            if w in [norm(a) for a in n.get("aliases") or []]:
                return nid
        return None

    # -- search
    def haystack(self, p):
        tags = p.get("t") or []
        parts = [p.get("s"), p.get("a")] + tags + ["#" + t for t in tags]
        parts += [self.title(t) for t in tags if t in self.nodes]
        return norm(" ".join(x for x in parts if x))

    def find(self, query):
        """Posts matching every word of the query (substring, case-insensitive); if none —
        posts matching any word, most matches first. Returns post indexes."""
        words = [w.lstrip("#") for w in WORD_RE.findall(norm(query))]
        words = [w for w in words if w]
        if not words:
            return []
        scored = []
        for i, p in enumerate(self.posts):
            h = self.haystack(p)
            hits = sum(1 for w in words if w in h)
            if hits:
                scored.append((hits, i))
        full = [i for hits, i in scored if hits == len(words)]
        if full:
            return full
        return [i for hits, i in sorted(scored, key=lambda x: (-x[0], x[1]))]

    def random_post(self, tag=None, rng=random):
        ids = self.post_ids(tag) if tag else list(range(len(self.posts)))
        return rng.choice(ids) if ids else None


def page_of(ids, page, size=PAGE):
    """(slice, page, pages) with page clamped into range."""
    pages = max(1, (len(ids) + size - 1) // size)
    page = min(max(0, int(page)), pages - 1)
    return ids[page * size:(page + 1) * size], page, pages


def btn(text, data):
    assert len(data.encode()) <= 64, data
    return {"text": text, "callback_data": data}


def rows(buttons, per_row=2):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def nonempty_children(idx, nid):
    return [k for k in idx.children(nid) if idx.count(k["id"])]


def tree_view(idx, nid=None):
    """(text, inline_keyboard) for the tree level under nid (None = root). Empty nodes are hidden."""
    kids = nonempty_children(idx, nid)
    buttons = [btn("%s (%d)" % (k.get("title") or k["id"], idx.count(k["id"])),
                   ("n:" + k["id"]) if nonempty_children(idx, k["id"]) else ("p:%s:0" % k["id"]))
               for k in kids]
    kb = rows(buttons)
    if nid is None:
        text = "🏷 Дерево тегов, постов в индексе: %d. Выбери раздел:" % len(idx.posts)
        if not kids:
            text = "Индекс пуст (0 постов с тегами). /reload — перечитать."
    else:
        crumbs = " › ".join(idx.title(t) for t in idx.path(nid))
        text = "🏷 %s — постов: %d" % (crumbs, idx.count(nid))
        nav = [btn("📄 все посты (%d)" % idx.count(nid), "p:%s:0" % nid)]
        up = idx.parent.get(nid)
        nav.append(btn("⬆️ вверх", "n:" + up if up else "n:"))
        kb.append(nav)
    return text, kb


def posts_nav(idx, ids, page, kind, key=None, label=""):
    """Navigation message shown after a page of copied posts.
    kind 'p' (tag key) or 'f' (last /find). Returns (text, keyboard, page_ids)."""
    chunk, page, pages = page_of(ids, page)
    first = page * PAGE + 1
    text = "%s: %d–%d из %d (стр. %d/%d)" % (label, first, first + len(chunk) - 1, len(ids), page + 1, pages) \
        if ids else "%s: ничего не нашлось" % label
    pref = ("p:%s:" % key) if kind == "p" else "f:"
    nav = []
    if page > 0:
        nav.append(btn("◀️ назад", pref + str(page - 1)))
    if page + 1 < pages:
        nav.append(btn("дальше ▶️", pref + str(page + 1)))
    kb = [nav] if nav else []
    if kind == "p":
        up = key if key in idx.nodes and nonempty_children(idx, key) else idx.parent.get(key)
        kb.append([btn("⬆️ вверх", "n:" + (up or ""))])
    return text, kb, [idx.posts[i]["m"] for i in chunk]


def parse_callback(data):
    """'p:pasta:2' -> ('p', 'pasta', 2); 'n:' -> ('n', None, 0); bad -> (None, None, 0)."""
    parts = (data or "").split(":")
    kind = parts[0]
    try:
        if kind == "n" and len(parts) == 2:
            return "n", parts[1] or None, 0
        if kind == "p" and len(parts) == 3:
            return "p", parts[1], int(parts[2])
        if kind == "f" and len(parts) == 2:
            return "f", None, int(parts[1])
        if kind == "r" and len(parts) == 2:
            return "r", parts[1] or None, 0
    except ValueError:
        pass
    return None, None, 0


# -- loading -------------------------------------------------------------------

def load_file(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "posts" not in data:
        raise ValueError("not an index.json (no 'posts')")
    return data


def fetch_index_github(repo, token, path="index.json", timeout=30):
    """Raw file from a private repo via GitHub contents API (fine-grained token, Contents: Read)."""
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/contents/%s" % (repo, path),
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github.raw",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "libinstabot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict) or "posts" not in data:
        raise ValueError("not an index.json (no 'posts')")
    return data


class IndexStore:
    """Holds the current Index. Source: GitHub (if GH_INDEX_TOKEN) every `every` seconds, cached
    into `path`; otherwise the file `path` (put into /data by the deploy workflow), re-read when
    its mtime changes. `reload()` forces a refresh."""

    def __init__(self, path, repo=None, token=None, every=600, logf=print):
        self.path, self.repo, self.token, self.every, self.log = path, repo, token, every, logf
        self.idx, self.loaded_at, self.mtime, self.source, self.error = Index({}), 0, None, "", ""

    def _from_file(self):
        data = load_file(self.path)
        self.mtime = os.path.getmtime(self.path)
        self.source = "file"
        return data

    def reload(self):
        """Returns (ok, message)."""
        try:
            if self.token and self.repo:
                try:
                    data = fetch_index_github(self.repo, self.token)
                    tmp = self.path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    os.replace(tmp, self.path)
                    self.mtime = os.path.getmtime(self.path)
                    self.source = "github"
                except Exception as exc:
                    self.log("index: github fetch failed (%s: %s), using cached file"
                             % (type(exc).__name__, str(exc)[:200]))
                    data = self._from_file()
                    self.source = "file (github недоступен)"
            else:
                data = self._from_file()
            self.idx = Index(data)
            self.loaded_at, self.error = time.time(), ""
            msg = "индекс: %d постов, %d разделов, собран %s UTC, источник %s" % (
                len(self.idx.posts), len(self.idx.tree),
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.idx.built_at)) if self.idx.built_at else "?",
                self.source)
            self.log(msg)
            return True, msg
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            self.loaded_at = time.time()      # do not retry in a tight loop
            self.log("index: load failed: " + self.error)
            return False, "индекс не загружен: " + self.error

    def maybe_reload(self):
        """Cheap periodic check from the service loop."""
        now = time.time()
        if self.token and self.repo:
            if now - self.loaded_at >= self.every:
                self.reload()
            return
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return
        if m != self.mtime and now - self.loaded_at >= 5:
            self.reload()
