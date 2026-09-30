"""Build tags_tree.yaml (SPEC §3.4) from already-tagged reels.

Planned to be re-run once, AFTER the full backlog is in the channel; until the
owner approves the tree, parse_channel.py runs with --l1-only.

    PYTHONPATH=. python tags_tree_build.py --db ~/reels-catalog/reels.db --out tags_tree.yaml
        [--per-cat-sample 60] [--max-posts N]

Pipeline (level 1 = the closed category set of categorize.py):
  1. load posts: categories, free tags, caption + transcript + visual;
  2. blacklist tags (hard rule): the leaked example of an old tags.py prompt
     (kettlebell/deadlift/home-workout) and any tag spread evenly over
     >= SPREAD_MIN_CATS categories (no category holds >= SPREAD_MAX_SHARE of it);
  3. per category the LLM proposes subtopics from a sample of post TEXTS
     (caption + transcript + visual), not from tags alone;
  4. every post of the category is assigned to one subtopic (or none) by the
     LLM from its caption + transcript + visual;
  5. hard rules (pure functions below, unit-tested):
     * node id: [a-z0-9_], at most MAX_ID_WORDS words, <= MAX_ID_LEN chars,
       not a level-1 id, unique in the tree;
     * a node keeps >= MIN_NODE_POSTS assigned posts, else it is dropped;
     * aliases of a node = free tags of its posts that are not blacklisted,
       seen >= ALIAS_MIN times in the node and whose home category (where the
       tag occurs most) is this category, so a node never overlaps another
       category; an alias is owned by one node only (the node with most hits).
"""

import argparse
import collections
import json
import os
import re
import sqlite3
import urllib.request

import yaml

# Level 1 = the closed category set of categorize.py (+ titles).
CATEGORY_TITLES = {
    "architecture": "Архитектура", "art": "Искусство", "beauty": "Красота",
    "cars": "Машины", "comedy": "Юмор", "cooking": "Готовка", "dance": "Танцы",
    "diy": "Своими руками", "education": "Обучение", "fashion": "Мода",
    "finance": "Финансы", "fitness": "Фитнес", "gaming": "Игры",
    "gardening": "Сад и огород", "health": "Здоровье", "motivation": "Мотивация",
    "music": "Музыка", "nature": "Природа", "pets": "Животные",
    "photography": "Фотография", "relationships": "Отношения",
    "science": "Наука", "sports": "Спорт", "tech": "Технологии",
    "travel": "Путешествия", "other": "Разное",
}


LEAKED = ("kettlebell", "deadlift", "home-workout")
MAX_ID_WORDS = 2
MAX_ID_LEN = 24
MIN_NODE_POSTS = 10
ALIAS_MIN = 2
SPREAD_MIN_CATS = 5
SPREAD_MAX_SHARE = 0.5
TEXT_LIMIT = 700

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
TRANSLATE_MODEL = os.environ.get("TREE_TRANSLATE_MODEL", "qwen2.5vl")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


# --------------------------------------------------------------------------- #
# Hard rules (pure).
# --------------------------------------------------------------------------- #

def valid_node_id(nid, taken=()):
    """id is [a-z0-9_]+, <= MAX_ID_WORDS words, <= MAX_ID_LEN chars, not taken."""
    return (bool(re.match(r"^[a-z0-9]+(_[a-z0-9]+)*$", nid or ""))
            and len(nid.split("_")) <= MAX_ID_WORDS and len(nid) <= MAX_ID_LEN
            and nid not in CATEGORY_TITLES and nid not in taken)


def tag_category_counts(posts):
    """{tag: Counter(category -> posts)} over the first category of each post."""
    out = collections.defaultdict(collections.Counter)
    for p in posts:
        for t in set(p["tags"]):
            out[t][p["cats"][0]] += 1
    return out


def blacklist(tag_cats, leaked=LEAKED, min_cats=SPREAD_MIN_CATS, max_share=SPREAD_MAX_SHARE):
    """Leaked example tags + tags spread evenly over >= min_cats categories."""
    bad = set(leaked)
    for t, cc in tag_cats.items():
        total = sum(cc.values())
        if len(cc) >= min_cats and max(cc.values()) < max_share * total:
            bad.add(t)
    return bad


def home_category(tag, tag_cats):
    cc = tag_cats.get(tag) or {}
    return max(sorted(cc), key=lambda c: cc[c]) if cc else None


def build_nodes(cat, subs, assignment, posts_by_id, tag_cats, bad,
                min_posts=MIN_NODE_POSTS, alias_min=ALIAS_MIN):
    """Apply the hard rules to one category.

    subs: [{"id","title"}] proposed subtopics (ids already validated);
    assignment: {post_id: sub_id or None}. Returns children, each with a
    "posts" count kept for reporting.
    """
    members = collections.defaultdict(list)
    for pid, sid in assignment.items():
        if sid is not None:
            members[sid].append(pid)
    hits = collections.defaultdict(collections.Counter)   # tag -> sid -> n
    for sid, pids in members.items():
        for pid in pids:
            for t in set(posts_by_id[pid]["tags"]):
                if t in bad or home_category(t, tag_cats) != cat:
                    continue
                hits[t][sid] += 1
    owner = {}
    for t, by in hits.items():
        sid, n = max(sorted(by.items()), key=lambda kv: kv[1])
        if n >= alias_min:
            owner[t] = sid
    children = []
    for s in subs:
        n = len(members.get(s["id"], []))
        if n < min_posts:
            continue
        al = sorted((t for t, o in owner.items() if o == s["id"] and slug(t) != s["id"]),
                    key=lambda t: (-hits[t][s["id"]], t))
        children.append({"id": s["id"], "title": s["title"], "aliases": al, "posts": n})
    return children


def post_text(p, limit=TEXT_LIMIT):
    parts = [p.get("caption"), p.get("transcript"), p.get("visual")]
    text = " | ".join(re.sub(r"\s+", " ", x).strip() for x in parts if x and x.strip())
    return text[:limit]


# --------------------------------------------------------------------------- #
# LLM + IO.
# --------------------------------------------------------------------------- #

def chat_json(system, user, model=None, temperature=0):
    payload = json.dumps({
        "model": model or MODEL, "stream": False, "format": "json",
        "options": {"temperature": temperature},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(OLLAMA_HOST.rstrip("/") + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        text = json.loads(resp.read())["message"]["content"]
    try:
        return json.loads(text)
    except ValueError:
        return {}


def load_posts(db_path):
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(reels)")}
    visual = "visual" if "visual" in cols else "NULL AS visual"
    posts = []
    for r in con.execute("SELECT pk, categories, tags, caption, transcript, %s FROM reels "
                         "WHERE tags IS NOT NULL" % visual):
        cats = [c for c in json.loads(r["categories"] or "[]") if c in CATEGORY_TITLES] or ["other"]
        posts.append({"id": r["pk"], "cats": cats, "tags": json.loads(r["tags"] or "[]"),
                      "caption": r["caption"], "transcript": r["transcript"],
                      "visual": r["visual"]})
    con.close()
    return posts


PROPOSE = (
    "You design subtopics for one top-level category of a personal library of "
    "Instagram reels. You get short texts (caption | transcript | visual scene) of "
    "sample reels of that category. Propose 3 to 8 subtopics that each cover MANY "
    "of the reels, are distinct and belong to this category only. Each id is 1 or "
    "2 lowercase English words in snake_case (it becomes a hashtag, e.g. pasta, "
    "street_food, strength). Return JSON with key subtopics: a list of objects "
    "with keys id and title (short English title)."
)

ASSIGN = (
    "Pick the one subtopic id from the list that this reel is about, judging by "
    "its caption, transcript and visual scene, or none if none fits well. "
    "Return JSON with one key subtopic: the id or none."
)

TRANSLATE = (
    "Translate each English title to a short natural Russian title (1-3 words). "
    "Return JSON with key ru: an object mapping each English title to Russian."
)


def propose(cat, sample, taken):
    for attempt in range(3):
        texts = "\n".join("- " + post_text(p, 300) for p in sample)
        res = chat_json(PROPOSE, "Category: %s\nReels:\n%s" % (cat, texts),
                        temperature=0 if attempt == 0 else 0.4)
        subs = []
        for s in res.get("subtopics") or []:
            if not isinstance(s, dict):
                continue
            sid = slug(s.get("id", ""))
            if valid_node_id(sid, set(taken) | {x["id"] for x in subs}):
                subs.append({"id": sid, "title": str(s.get("title") or sid)})
        if len(subs) >= 2:
            return subs
    return []


def build(db_path, per_cat_sample=60, max_posts=None, verbose=True):
    import random
    posts = load_posts(db_path)
    by_id = {p["id"]: p for p in posts}
    tag_cats = tag_category_counts(posts)
    bad = blacklist(tag_cats)
    if verbose:
        print("posts %d, blacklisted tags %d: %s" % (
            len(posts), len(bad), ", ".join(sorted(bad)[:30])), flush=True)
    by_cat = collections.defaultdict(list)
    for p in posts:
        by_cat[p["cats"][0]].append(p)
    taken = set()
    tree = []
    for cat in sorted(CATEGORY_TITLES, key=lambda c: -len(by_cat.get(c, []))):
        node = {"id": cat, "title": CATEGORY_TITLES[cat]}
        mine = [p for p in by_cat.get(cat, []) if post_text(p)]
        if len(mine) < MIN_NODE_POSTS:
            tree.append(node)
            continue
        rnd = random.Random(cat)
        subs = propose(cat, rnd.sample(mine, min(per_cat_sample, len(mine))), taken)
        if not subs:
            tree.append(node)
            continue
        ids = [x["id"] for x in subs]
        assignment = {}
        for p in (mine if max_posts is None else mine[:max_posts]):
            res = chat_json(ASSIGN, "Subtopics: %s\nReel: %s" % (", ".join(ids), post_text(p)))
            sid = slug(res.get("subtopic", ""))
            assignment[p["id"]] = sid if sid in ids else None
        children = build_nodes(cat, subs, assignment, by_id, tag_cats, bad)
        if children:
            ru = chat_json(TRANSLATE, "\n".join(c["title"] for c in children),
                           model=TRANSLATE_MODEL).get("ru") or {}
            if verbose:
                print("%-13s %4d posts -> %s" % (cat, len(assignment), ", ".join(
                    "%s(%d)" % (c["id"], c["posts"]) for c in children)), flush=True)
            for c in children:
                c["title"] = str(ru.get(c["title"]) or c["title"])
                taken.add(c["id"])
                c.pop("posts")
            node["children"] = children
        tree.append(node)
    return tree


HEADER = (
    "# Дерево тегов libinsta (SPEC §3.4), собрано tags_tree_build.py.\n"
    "# ЧЕРНОВИК до утверждения владельцем: parse_channel.py идёт с --l1-only.\n"
    "# id = хэштег ([a-z0-9_]+, не больше 2 слов). aliases: свободные теги tags.py,\n"
    "# которые схлопываются в узел. Узел L2: не меньше 10 постов своей категории.\n"
)


def dump(tree, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(tree, f, allow_unicode=True, sort_keys=False, width=100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("REELS_DB", "reels.db"))
    ap.add_argument("--out", default="tags_tree.yaml")
    ap.add_argument("--per-cat-sample", type=int, default=60)
    ap.add_argument("--max-posts", type=int, help="per category, for quick trials")
    args = ap.parse_args()
    dump(build(args.db, args.per_cat_sample, args.max_posts), args.out)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
