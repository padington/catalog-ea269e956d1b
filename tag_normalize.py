"""Tag normalization against tags_tree.yaml (SPEC §3.4).

Pure functions, no network. Turns a reel's raw `categories` (closed set from
categorize.py) and free `tags` (tags.py) into the final list of hashtag ids:

* category -> level-1 node with the same id (unknown categories are dropped);
* free tag -> node whose id or `aliases` match it (case/`-`/`_` insensitive),
  plus that node's ancestors, so the bot can count posts per subtree;
* unmatched tag -> `misc_<slug>` if it occurs >= MISC_MIN times in the corpus
  (`counts`, keyed by key(tag) — see tag_counts), otherwise dropped;
* a node with `only_with_parent: true` is kept only if the post already has its
  level-1 parent among its categories (guards against the leaked
  kettlebell/deadlift/home-workout example of an old tags.py prompt).

Order of the result: categories first (in their original order), then tag
nodes in the order the raw tags came, ancestors before children, no dupes.
"""

import os
import re

ID_RE = re.compile(r"^[a-z0-9_]+$")
MAX_DEPTH = 3
MISC_MIN = 5
DEFAULT_TREE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tags_tree.yaml")


def key(tag):
    """Lookup key for a raw tag / alias: lowercase, runs of non-alnum -> '_'."""
    return re.sub(r"[^0-9a-zа-яё]+", "_", str(tag).strip().lower()).strip("_")


def slug(tag):
    """Hashtag-safe slug ([a-z0-9_]); '' if nothing ascii is left."""
    return re.sub(r"[^a-z0-9]+", "_", str(tag).strip().lower()).strip("_")


def load_tree(path=DEFAULT_TREE):
    import yaml  # only needed when reading the yaml file
    with open(path, encoding="utf-8") as f:
        tree = yaml.safe_load(f) or []
    validate_tree(tree)
    return tree


def validate_tree(tree):
    """Raise ValueError on bad ids, duplicate ids or depth > MAX_DEPTH."""
    seen = set()

    def walk(nodes, depth):
        if nodes and depth > MAX_DEPTH:
            raise ValueError("tree deeper than %d levels" % MAX_DEPTH)
        for n in nodes or []:
            nid = n.get("id")
            if not isinstance(nid, str) or not ID_RE.match(nid):
                raise ValueError("bad node id: %r" % (nid,))
            if nid in seen:
                raise ValueError("duplicate node id: %s" % nid)
            seen.add(nid)
            walk(n.get("children"), depth + 1)

    walk(tree, 1)


class TreeIndex(object):
    """Lookup tables over a validated tree."""

    def __init__(self, tree):
        validate_tree(tree)
        self.tree = tree
        self.parent = {}        # id -> parent id (None for level 1)
        self.level1 = set()
        self.only_with_parent = set()
        self.lookup = {}        # key(alias or id) -> node id
        self._walk(tree, None)

    def _walk(self, nodes, parent):
        for n in nodes or []:
            nid = n["id"]
            self.parent[nid] = parent
            if parent is None:
                self.level1.add(nid)
            if n.get("only_with_parent"):
                self.only_with_parent.add(nid)
            # ids win over aliases: set id first, aliases never overwrite an id
            self.lookup[key(nid)] = nid
            self._walk(n.get("children"), nid)
        for n in nodes or []:
            for a in n.get("aliases") or []:
                self.lookup.setdefault(key(a), n["id"])

    def ancestors(self, nid):
        """[root, ..., nid]."""
        chain = []
        while nid is not None:
            chain.append(nid)
            nid = self.parent.get(nid)
        return chain[::-1]

    def root(self, nid):
        return self.ancestors(nid)[0]


def normalize(categories, tags, index, counts=None, misc_min=MISC_MIN):
    """-> ordered list of hashtag ids (without '#')."""
    out = []

    def add(x):
        if x and x not in out:
            out.append(x)

    cats = [key(c) for c in (categories or [])]
    for c in cats:
        if c in index.level1:
            add(c)
    counts = counts or {}
    for raw in tags or []:
        k = key(raw)
        if not k:
            continue
        nid = index.lookup.get(k)
        if nid is not None:
            if nid in index.only_with_parent and index.root(nid) not in cats:
                continue
            for a in index.ancestors(nid):
                add(a)
            continue
        s = slug(raw)
        if s and counts.get(k, 0) >= misc_min:
            add("misc_" + s)
    return out


def hashtag_line(ids):
    return " ".join("#" + i for i in ids)


def tag_counts(tag_lists):
    """Corpus frequency of raw tags, keyed by key(tag). `tag_lists`: iterable of lists."""
    counts = {}
    for tags in tag_lists:
        for t in tags or []:
            k = key(t)
            if k:
                counts[k] = counts.get(k, 0) + 1
    return counts
