"""backlog.py pure logic + db §3.3 migration. In-memory sqlite, no network."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backlog
import db as dbm


def _conn(rows):
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    for pk, taken in rows:
        dbm.upsert_reel(conn, {"pk": pk, "shortcode": "c" + pk, "shared_by": "me",
                               "caption": "cap " + pk, "taken_at": taken})
    return conn


class MigrationTest(unittest.TestCase):
    def test_adds_columns_to_old_db_losslessly(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "reels.db")
        c = sqlite3.connect(path)
        c.execute(dbm.SCHEMA)
        c.execute("INSERT INTO reels (pk, caption) VALUES ('1', 'x')")
        c.commit(); c.close()
        conn = dbm.connect(path)
        self.assertIn("tg_message_id", dbm.missing_tg_columns(conn))
        conn.close()
        bak = dbm.backup_db(path, "t")
        self.assertTrue(os.path.exists(bak))
        conn = dbm.connect(path)
        added = dbm.migrate_tg_columns(conn)
        for col in ("tg_message_id", "tg_file_id", "tg_kind", "tg_posted_at", "tags_final", "parsed_at"):
            self.assertIn(col, added)
        self.assertEqual(dbm.missing_tg_columns(conn), [])
        self.assertEqual(dbm.migrate_tg_columns(conn), [])  # idempotent
        self.assertEqual(conn.execute("SELECT caption FROM reels WHERE pk='1'").fetchone()[0], "x")
        self.assertEqual(sqlite3.connect(bak).execute("SELECT COUNT(*) FROM reels").fetchone()[0], 1)


class SelectTest(unittest.TestCase):
    def test_newest_first_and_skips_posted_and_final(self):
        conn = _conn([("1", 100), ("2", 300), ("3", 200), ("4", 400), ("5", 50)])
        backlog.mark_posted(conn, "4", 7)
        conn.execute("UPDATE reels SET tg_status='no_video' WHERE pk='2'")
        conn.execute("UPDATE reels SET tg_status='throttled' WHERE pk='3'")
        self.assertEqual([r["pk"] for r in backlog.select_batch(conn, 10)], ["3", "1", "5"])
        self.assertEqual([r["pk"] for r in backlog.select_batch(conn, 2)], ["3", "1"])
        self.assertEqual([r["pk"] for r in backlog.select_batch(conn, 10, ("no_video",))], ["2", "3", "1", "5"])

    def test_size_capped(self):
        conn = _conn([(str(i), i) for i in range(200)])
        self.assertEqual(len(backlog.select_batch(conn, 1000)), backlog.MAX_BATCH)

    def test_payload(self):
        conn = _conn([("1", 1)])
        p = backlog.batch_payload(backlog.select_batch(conn, 5))
        self.assertEqual(p, [{"pk": "1", "shortcode": "c1", "shared_by": "me", "caption": "cap 1"}])


class ApplyTest(unittest.TestCase):
    def test_parse_last_wins_and_junk(self):
        t = '{"pk": "1", "status": "failed"}\nnot json\n\n{"pk": "1", "status": "sent", "message_id": 9}\n'
        self.assertEqual(backlog.parse_results(t)["1"]["status"], "sent")

    def test_apply(self):
        conn = _conn([("1", 1), ("2", 2), ("3", 3), ("4", 4)])
        backlog.mark_posted(conn, "4", 5, "F4")
        recs = {
            "1": {"pk": "1", "status": "sent", "message_id": 10, "file_id": "F1"},
            "2": {"pk": "2", "status": "no_video"},
            "3": {"pk": "3", "status": "throttled", "error": "429"},
            "4": {"pk": "4", "status": "failed", "error": "boom"},
        }
        counts = backlog.apply_results(conn, recs, now=123)
        self.assertEqual(counts, {"sent": 1, "no_video": 1, "throttled": 1, "failed": 1})
        row = lambda pk: dict(conn.execute("SELECT * FROM reels WHERE pk=?", (pk,)).fetchone())
        r1 = row("1")
        self.assertEqual((r1["tg_message_id"], r1["tg_file_id"], r1["tg_kind"], r1["tg_posted_at"], r1["tg_status"]),
                         (10, "F1", "video", 123, "sent"))
        self.assertIsNone(row("2")["tg_message_id"]); self.assertEqual(row("2")["tg_status"], "no_video")
        self.assertEqual(row("3")["tg_error"], "429")
        r4 = row("4")  # posted row is not clobbered by a later failure
        self.assertEqual((r4["tg_message_id"], r4["tg_status"]), (5, "manual"))

    def test_next_delay_and_report(self):
        self.assertEqual(backlog.next_delay(6, True), 9)
        self.assertEqual(backlog.next_delay(6, False), 6)
        conn = _conn([("1", 1), ("2", 2)])
        backlog.apply_results(conn, {"1": {"pk": "1", "status": "sent", "message_id": 3},
                                     "2": {"pk": "2", "status": "failed", "error": "tg: bad"}})
        md = backlog.report_md(conn)
        self.assertIn("in channel: **1**", md)
        self.assertIn("`2`", md); self.assertIn("tg: bad", md)


if __name__ == "__main__":
    unittest.main()
