"""Build index.json for the bot's search (SPEC §3.5) and publish it to GitHub.

    PYTHONPATH=. python index_build.py [--out index.json] [--no-publish]

Source: the `tg_posts` table written by parse_channel.py (message_id, kind,
tags, author, date, caption) joined with `reels` (caption + transcript for the
search snippet). Only posts with a parsed_at (i.e. tagged) are indexed.

Publishing: private repo `padington/libinsta-index`, file `index.json`, via a
local git clone in `.libinsta-index/` (gh auth). Nothing is pushed when the
content (ignoring built_at) did not change.
"""

import argparse
import json
import os
import re
import subprocess
import time

import tag_normalize as tn

INDEX_REPO = os.environ.get("LIBINSTA_INDEX_REPO", "padington/libinsta-index")
HERE = os.path.dirname(os.path.abspath(__file__))
SNIPPET = 200


def snippet(*parts, limit=SNIPPET):
    text = " ".join(re.sub(r"\s+", " ", p or "").strip() for p in parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def caption_text(caption):
    """IG text part of a channel caption (before the instagram link line)."""
    out = []
    for ln in (caption or "").splitlines():
        if "instagram.com/" in ln:
            break
        out.append(ln)
    return "\n".join(out).strip()


def post_entry(p, reel=None):
    """One §3.5 post record from a tg_posts row (+ optional reels row)."""
    reel = reel or {}
    tags = json.loads(p.get("tags") or "[]")
    text = caption_text(p.get("caption")) or reel.get("caption") or ""
    return {
        "m": p["message_id"],
        "k": p.get("kind") or "video",
        "t": tags,
        "d": p.get("date") or "",
        "a": p.get("author") or "",
        "s": snippet(text, reel.get("transcript") or ""),
    }


def build_index(conn, tree, now=None):
    tn.validate_tree(tree)
    rows = conn.execute(
        "SELECT * FROM tg_posts WHERE parsed_at IS NOT NULL ORDER BY message_id").fetchall()
    posts = []
    for r in rows:
        p = dict(r)
        reel = None
        if p.get("pk"):
            rr = conn.execute("SELECT caption, transcript FROM reels WHERE pk = ?",
                              (p["pk"],)).fetchone()
            reel = dict(rr) if rr else None
        posts.append(post_entry(p, reel))
    return {"version": 1, "built_at": int(now or time.time()), "tree": tree, "posts": posts}


def write_index(idx, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
    return path


def _same_content(a_path, idx):
    try:
        with open(a_path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return False
    old.pop("built_at", None)
    new = dict(idx)
    new.pop("built_at", None)
    return old == new


def _identity(workdir):
    """git -c user.* flags from the gh account when git has no identity."""
    have = subprocess.run(["git", "-C", workdir, "config", "user.email"],
                          capture_output=True, text=True).stdout.strip()
    if have:
        return []
    out = subprocess.run(["gh", "api", "user", "--jq", ".id,.login"],
                         capture_output=True, text=True, check=True).stdout.split()
    uid, login = out[0], out[1]
    return ["-c", "user.name=" + login,
            "-c", "user.email=%s+%s@users.noreply.github.com" % (uid, login)]


def publish(path, repo=INDEX_REPO, workdir=None):
    """Commit index.json into the private index repo and push (if changed)."""
    workdir = workdir or os.path.join(HERE, ".libinsta-index")
    if not os.path.isdir(os.path.join(workdir, ".git")):
        subprocess.run(["gh", "repo", "clone", repo, workdir], check=True)
    else:
        subprocess.run(["git", "-C", workdir, "pull", "-q", "--ff-only"], check=False)
    with open(path, encoding="utf-8") as f:
        idx = json.load(f)
    dst = os.path.join(workdir, "index.json")
    if not _same_content(dst, idx):  # built_at alone is not a change
        write_index(idx, dst)
    subprocess.run(["git", "-C", workdir, "add", "index.json"], check=True)
    staged = subprocess.run(["git", "-C", workdir, "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        print("index unchanged, nothing to push", flush=True)
        return False
    subprocess.run(["git", "-C", workdir] + _identity(workdir) + ["commit", "-q", "-m",
                    "index: %d posts" % len(idx["posts"])], check=True)
    branch = subprocess.run(["git", "-C", workdir, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    if branch in ("", "HEAD"):
        branch = "main"
    subprocess.run(["git", "-C", workdir, "push", "-q", "origin", "HEAD:" + branch], check=True)
    print("pushed index.json (%d posts) to %s" % (len(idx["posts"]), repo), flush=True)
    return True


def main():
    import db as dbm
    import parse_channel
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "index.json"))
    ap.add_argument("--tree", default=tn.DEFAULT_TREE)
    ap.add_argument("--no-publish", action="store_true")
    args = ap.parse_args()
    conn = dbm.connect(os.environ.get("REELS_DB", os.path.join(HERE, "reels.db")))
    parse_channel.init_tg_posts(conn)
    idx = build_index(conn, tn.load_tree(args.tree))
    write_index(idx, args.out)
    print("index: %d post(s) -> %s" % (len(idx["posts"]), args.out))
    if not args.no_publish:
        publish(args.out)


if __name__ == "__main__":
    main()
