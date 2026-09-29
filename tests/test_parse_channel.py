import json
import sqlite3
import unittest

import index_build
import parse_channel as pc

CAP = ("Паста за 15 минут 🍝\nрецепт внизу\n\n"
       "https://www.instagram.com/reel/DBx9_a-Z1q2/\n"
       "@chef.anna · 2026-06-13 · from me\n#nonparsed")


class LastLine(unittest.TestCase):
    def test_is_nonparsed(self):
        self.assertTrue(pc.is_nonparsed(CAP))
        self.assertTrue(pc.is_nonparsed(CAP + "\n\n"))
        self.assertFalse(pc.is_nonparsed(CAP.replace("#nonparsed", "#cooking")))
        self.assertFalse(pc.is_nonparsed("text #nonparsed"))
        self.assertFalse(pc.is_nonparsed(""))

    def test_replace_only_last_line(self):
        new = pc.replace_last_line(CAP, ["cooking", "pasta"])
        head = CAP.rsplit("\n", 1)[0]
        self.assertEqual(new, head + "\n#cooking #pasta")

    def test_is_retaggable(self):
        self.assertTrue(pc.is_retaggable("x\n#music #art"))
        self.assertTrue(pc.is_retaggable(CAP))
        self.assertFalse(pc.is_retaggable("x\n#unavailable"))
        self.assertFalse(pc.is_retaggable("x\n#music and text"))

    def test_replace_single_line_caption(self):
        self.assertEqual(pc.replace_last_line("#nonparsed", ["other"]), "#other")

    def test_limit_trims_ig_text_keeps_link_meta_and_tags(self):
        body = ("x" * 1000 + "…\n\nhttps://www.instagram.com/reel/ABC/\n"
                "@a · 2026-01-01 · from me\n#nonparsed")
        new = pc.replace_last_line(body, ["cooking", "pasta", "baking"])
        self.assertLessEqual(pc.tg_len(new), 1024)
        self.assertTrue(new.endswith("\n\nhttps://www.instagram.com/reel/ABC/\n"
                                     "@a · 2026-01-01 · from me\n#cooking #pasta #baking"))
        self.assertTrue(new.startswith("xxx"))
        self.assertIn("…\n\nhttps://", new)

    def test_limit_drops_trailing_tags_keeps_category(self):
        body = "x" * 1000 + "\n#nonparsed"
        new = pc.replace_last_line(body, ["cooking", "pasta", "baking", "bread"])
        self.assertLessEqual(pc.tg_len(new), 1024)
        self.assertTrue(new.startswith("x" * 1000 + "\n#cooking"))
        self.assertNotIn("#bread", new)
        full = pc.replace_last_line(body, ["other"], limit=10)
        self.assertTrue(full.endswith("\n#other"))

    def test_tg_len_utf16(self):
        self.assertEqual(pc.tg_len("🍝"), 2)
        self.assertEqual(pc.tg_len("ы"), 1)


class Caption(unittest.TestCase):
    def test_parse(self):
        m = pc.parse_caption(CAP)
        self.assertEqual(m["shortcode"], "DBx9_a-Z1q2")
        self.assertEqual(m["url"], "https://www.instagram.com/reel/DBx9_a-Z1q2/")
        self.assertEqual((m["author"], m["date"], m["shared_by"]), ("chef.anna", "2026-06-13", "me"))
        self.assertEqual(m["text"], "Паста за 15 минут 🍝\nрецепт внизу")

    def test_parse_post_without_from(self):
        m = pc.parse_caption("https://www.instagram.com/p/ABC/\n@a_b · 2025-01-02\n#nonparsed")
        self.assertEqual((m["shortcode"], m["url"], m["author"], m["shared_by"], m["text"]),
                         ("ABC", "https://www.instagram.com/p/ABC/", "a_b", None, ""))

    def test_media_pk_from_code(self):
        # value from instagrapi.media_pk_from_code
        self.assertEqual(pc.media_pk_from_code("B"), "1")
        self.assertEqual(pc.media_pk_from_code("BA"), "64")
        self.assertEqual(pc.media_pk_from_code("CRGwCJHH0ol"), "2613987887189805605")
        self.assertEqual(pc.media_pk_from_code("DBx9_a-Z1q2"), "3490843825317305014")


class Index(unittest.TestCase):
    def test_build_index(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE reels (pk TEXT PRIMARY KEY, caption TEXT, transcript TEXT)")
        conn.execute("INSERT INTO reels VALUES ('1', 'ig caption', 'spoken  words')")
        pc.init_tg_posts(conn)
        pc.upsert_tg_post(conn, 10, pk="1", kind="video", author="chef.anna", date="2026-06-13",
                          caption=pc.replace_last_line(CAP, ["cooking"]),
                          tags=json.dumps(["cooking", "pasta"]), parsed_at=5)
        pc.upsert_tg_post(conn, 11, pk="2", kind="video", caption=CAP)  # not parsed yet
        tree = [{"id": "cooking", "title": "Готовка"}]
        idx = index_build.build_index(conn, tree, now=123)
        self.assertEqual(idx["version"], 1)
        self.assertEqual(idx["built_at"], 123)
        self.assertEqual(idx["tree"], tree)
        self.assertEqual(idx["posts"], [{
            "m": 10, "k": "video", "t": ["cooking", "pasta"], "d": "2026-06-13",
            "a": "chef.anna", "s": "Паста за 15 минут 🍝 рецепт внизу spoken words"}])

    def test_snippet_limit(self):
        self.assertEqual(len(index_build.snippet("a" * 150, "b" * 150)), 200)


if __name__ == "__main__":
    unittest.main()
