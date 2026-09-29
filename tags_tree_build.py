"""Build the first version of tags_tree.yaml from already-tagged reels (SPEC §3.4).

One-off helper (iteration 3). Reads reels.db READ-ONLY, counts categories and
free tags, then asks the local LLM (ollama, llama3.2) to
  1. propose 4-10 level-2 subtopics per category from its most frequent tags;
  2. assign every tag seen in that category to one subtopic (-> aliases).
The result is a starting point: the owner edits tags_tree.yaml by hand later.

    PYTHONPATH=. python tags_tree_build.py --db ~/reels-catalog/reels.db --out tags_tree.yaml

Known data quirk: an old tags.py prompt used ["kettlebell","deadlift",
"home-workout"] as its example, and the model copied it into ~650 unrelated
reels. Those tags are counted only for `fitness` posts and get their own
fitness children with `only_with_parent: true` (see tag_normalize.py).
"""

import argparse
import collections
import json
import os
import re
import sqlite3
import urllib.request

import yaml

LEAKED = ("kettlebell", "deadlift", "home-workout")
LEAKED_TITLES = {"kettlebell": "Гири", "deadlift": "Становая тяга", "home-workout": "Тренировки дома"}

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

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    return s


TRANSLATE_MODEL = os.environ.get("TREE_TRANSLATE_MODEL", "qwen2.5vl")


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


def load_counts(db_path):
    """-> (cat_counts, per_cat_tag_counts, global_tag_counts)."""
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    cats = collections.Counter()
    per_cat = collections.defaultdict(collections.Counter)
    glob = collections.Counter()
    for c, t in con.execute(
            "SELECT categories, tags FROM reels WHERE tags IS NOT NULL AND tags != '[]'"):
        cs = json.loads(c or "[]") or ["other"]
        for x in cs:
            cats[x] += 1
        for tag in json.loads(t):
            if tag in LEAKED and "fitness" not in cs:
                continue
            glob[tag] += 1
            per_cat[cs[0]][tag] += 1
    con.close()
    return cats, per_cat, glob


def home_category(tag, per_cat):
    return max(per_cat, key=lambda c: (per_cat[c][tag], c == "other") if tag in per_cat[c] else (-1, False))


PROPOSE = (
    "You design a tag taxonomy for a personal library of Instagram reels. Given "
    "one top-level category and the tags seen in it (most frequent first), "
    "propose 4 to 10 subtopics that together cover most of the tags. Subtopics "
    "must be broad (each should fit several tags), distinct, and specific to the "
    "category. Each id is 1-2 lowercase English words in snake_case, at most 20 "
    "characters (it becomes a hashtag, e.g. pasta, street_food, strength). "
    "Return JSON {\"subtopics\":[{\"id\":\"...\",\"title\":\"short English title\"}]}."
)

ASSIGN = (
    "Assign each tag to exactly one subtopic id from the list, or to \"none\" if "
    "no subtopic fits well. Return JSON {\"assign\":{\"<tag>\":\"<subtopic id or none>\"}} "
    "containing every input tag."
)

TRANSLATE = (
    "Translate each English title to a short natural Russian title (1-3 words). "
    "Return JSON {\"ru\":{\"<english>\":\"<russian>\"}}."
)


def build(db_path, min_tag=1, max_tags_per_cat=160, verbose=True):
    cats, per_cat, glob = load_counts(db_path)
    home = {}
    for tag in glob:
        home[tag] = home_category(tag, per_cat)
    tree = []
    used = set(CATEGORY_TITLES) | {slug(t) for t in LEAKED}
    for cat in sorted(CATEGORY_TITLES, key=lambda c: -cats.get(c, 0)):
        node = {"id": cat, "title": CATEGORY_TITLES[cat]}
        mine = [t for t, n in glob.most_common() if home.get(t) == cat and n >= min_tag]
        mine = mine[:max_tags_per_cat]
        fixed = []
        if cat == "fitness":
            # leaked example tags get their own guarded nodes (see module doc)
            fixed = [{"id": slug(t), "title": LEAKED_TITLES[t], "aliases": [t],
                      "only_with_parent": True} for t in LEAKED]
            mine = [t for t in mine if t not in LEAKED]
        if len(mine) < 3:
            if fixed:
                node["children"] = fixed
            tree.append(node)
            continue
        subs = []
        for attempt in range(3):
            prop = chat_json(PROPOSE, "Category: %s\nTags: %s" % (cat, ", ".join(mine[:70])),
                             temperature=0 if attempt == 0 else 0.4)
            subs = []
            for s in prop.get("subtopics") or []:
                if not isinstance(s, dict):
                    continue
                sid = slug(s.get("id", ""))[:24].strip("_")
                if sid in used:
                    sid = ("%s_%s" % (cat, sid))[:32].strip("_")
                if sid and sid not in used and sid not in [x["id"] for x in subs]:
                    subs.append({"id": sid, "title": str(s.get("title") or sid)})
            if len(subs) >= 3:
                break
        if not subs:
            tree.append(node)
            continue
        aliases = collections.defaultdict(list)
        ids = [s["id"] for s in subs]
        for i in range(0, len(mine), 40):
            chunk = mine[i:i + 40]
            res = chat_json(ASSIGN, "Subtopics: %s\nTags: %s" % (", ".join(ids), ", ".join(chunk)))
            for tag, sid in (res.get("assign") or {}).items():
                sid = slug(sid)
                if tag in chunk and sid in ids:
                    aliases[sid].append(tag)
        ru = chat_json(TRANSLATE, "\n".join(s["title"] for s in subs),
                       model=TRANSLATE_MODEL).get("ru") or {}
        children = list(fixed)
        for s in subs:
            al = sorted(set(aliases[s["id"]]) - {s["id"]}, key=lambda t: (-glob[t], t))
            if not al:
                continue
            child = {"id": s["id"], "title": str(ru.get(s["title"]) or s["title"]),
                     "aliases": al}
            children.append(child)
            used.add(s["id"])
        if children:
            node["children"] = children
        tree.append(node)
        if verbose:
            print("%-13s %3d tags -> %d subtopics, %d aliased" % (
                cat, len(mine), len(children), sum(len(c["aliases"]) for c in children)), flush=True)
    return tree


HEADER = (
    "# Дерево тегов libinsta (SPEC §3.4). Первая версия собрана tags_tree_build.py\n"
    "# из размеченных постов reels.db (llama3.2), дальше правится руками.\n"
    "# id = хэштег ([a-z0-9_]+). aliases — свободные теги tags.py, которые\n"
    "# схлопываются в узел. only_with_parent: true — тег засчитывается, только\n"
    "# если у поста есть родительская категория (утечка примера старого промпта).\n"
)


def dump(tree, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(tree, f, allow_unicode=True, sort_keys=False, width=100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("REELS_DB", "reels.db"))
    ap.add_argument("--out", default="tags_tree.yaml")
    ap.add_argument("--min-tag", type=int, default=1)
    args = ap.parse_args()
    tree = build(args.db, min_tag=args.min_tag)
    dump(tree, args.out)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
