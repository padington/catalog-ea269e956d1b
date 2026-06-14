"""Wrap the pre-existing module self-tests so they run under unittest too.

These mirror `python db.py` and `python categorize.py --selftest`; keeping them
here lets the whole suite run via the venv test runner.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db as dbm
import categorize


class DbSelfTest(unittest.TestCase):
    def test_db_smoke(self):
        c = dbm.connect(":memory:")
        dbm.init_db(c)
        dbm.upsert_reel(c, {"pk": "1", "shortcode": "abc", "url": "u",
                            "source": "saved", "caption": "hello",
                            "thumbnail_url": "t", "taken_at": 0})
        dbm.upsert_reel(c, {"pk": "1", "shortcode": "abc"})  # ignored dup
        self.assertEqual(len(dbm.all_reels(c)), 1)
        self.assertEqual([r["pk"] for r in dbm.iter_uncategorized(c)], ["1"])
        dbm.set_categories(c, "1", ["other"])
        self.assertEqual(list(dbm.iter_uncategorized(c)), [])
        self.assertEqual(dbm.all_reels(c)[0]["categories"], ["other"])


class CategorizeSelfTest(unittest.TestCase):
    def test_stub_backend(self):
        self.assertEqual(
            categorize._stub_backend("Great dumbbell workout for arms"),
            ["fitness"],
        )
        self.assertEqual(categorize._stub_backend("Easy pasta recipe"),
                         ["cooking"])
        self.assertEqual(categorize._stub_backend(""), ["other"])


if __name__ == "__main__":
    unittest.main()
