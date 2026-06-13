import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    pk            TEXT PRIMARY KEY,
    shortcode     TEXT,
    url           TEXT,
    source        TEXT,
    shared_by     TEXT,
    caption       TEXT,
    thumbnail_url TEXT,
    taken_at      INTEGER,
    categories    TEXT,
    created_at    INTEGER
)
"""


def connect(path="reels.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.execute(SCHEMA)
    conn.commit()


def upsert_reel(conn, reel):
    # INSERT OR IGNORE on pk keeps re-runs resumable and never clobbers an
    # already-categorized row.
    conn.execute(
        """
        INSERT OR IGNORE INTO reels
            (pk, shortcode, url, source, shared_by, caption,
             thumbnail_url, taken_at, categories, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reel["pk"],
            reel.get("shortcode"),
            reel.get("url"),
            reel.get("source"),
            reel.get("shared_by"),
            reel.get("caption"),
            reel.get("thumbnail_url"),
            reel.get("taken_at"),
            None,
            int(time.time()),
        ),
    )
    conn.commit()


def iter_uncategorized(conn):
    cur = conn.execute("SELECT * FROM reels WHERE categories IS NULL")
    for row in cur:
        yield dict(row)


_UPDATABLE = ("shortcode", "url", "caption", "thumbnail_url", "taken_at", "categories")


def update_reel(conn, pk, fields):
    # UPDATE only whitelisted columns for a pk; column names are validated
    # against _UPDATABLE (never interpolated from caller input) and values are
    # parameterized, so this is SQL-injection-safe.
    cols = [c for c in fields if c in _UPDATABLE]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)
    values = [fields[c] for c in cols]
    conn.execute(
        f"UPDATE reels SET {assignments} WHERE pk = ?",
        (*values, pk),
    )
    conn.commit()


def set_categories(conn, pk, categories):
    conn.execute(
        "UPDATE reels SET categories = ? WHERE pk = ?",
        (json.dumps(categories), pk),
    )
    conn.commit()


def all_reels(conn):
    cur = conn.execute("SELECT * FROM reels ORDER BY taken_at DESC")
    out = []
    for row in cur:
        d = dict(row)
        d["categories"] = json.loads(d["categories"]) if d["categories"] else []
        out.append(d)
    return out


if __name__ == "__main__":
    # Smoke test with an in-memory DB, no third-party deps.
    c = connect(":memory:")
    init_db(c)
    upsert_reel(c, {"pk": "1", "shortcode": "abc", "url": "u", "source": "saved",
                    "caption": "hello", "thumbnail_url": "t", "taken_at": 0})
    upsert_reel(c, {"pk": "1", "shortcode": "abc"})  # ignored duplicate
    assert len(all_reels(c)) == 1
    assert [r["pk"] for r in iter_uncategorized(c)] == ["1"]
    set_categories(c, "1", ["other"])
    assert list(iter_uncategorized(c)) == []
    assert all_reels(c)[0]["categories"] == ["other"]
    print("db.py self-test ok")
