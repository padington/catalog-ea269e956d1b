import unittest

import parse_channel as pc
import tag_normalize as tn
import tags_tree_build as tb


def post(pid, cat, tags):
    return {"id": pid, "cats": [cat], "tags": tags}


class NodeId(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(tb.valid_node_id("pasta"))
        self.assertTrue(tb.valid_node_id("street_food"))
        self.assertFalse(tb.valid_node_id("barbell_and_dumbbell"))   # 3 words
        self.assertFalse(tb.valid_node_id("cooking"))                # level-1 id
        self.assertFalse(tb.valid_node_id("pasta", taken={"pasta"}))
        self.assertFalse(tb.valid_node_id("Pasta"))
        self.assertFalse(tb.valid_node_id("a" * 25))
        self.assertFalse(tb.valid_node_id(""))


class Blacklist(unittest.TestCase):
    def test_leaked_and_spread(self):
        posts = [post(i, c, ["kino"]) for i, c in enumerate(
            ["other", "comedy", "art", "music", "travel", "cooking"])]
        posts += [post(10 + i, "cooking", ["pasta"]) for i in range(6)]
        posts += [post(20, "art", ["pasta"]), post(21, "music", ["pasta"]),
                  post(22, "travel", ["pasta"]), post(23, "comedy", ["pasta"])]
        bad = tb.blacklist(tb.tag_category_counts(posts))
        self.assertIn("kino", bad)            # even over 6 categories
        self.assertNotIn("pasta", bad)        # 5 categories but 60% in cooking
        self.assertTrue({"kettlebell", "deadlift", "home-workout"} <= bad)


class BuildNodes(unittest.TestCase):
    def setUp(self):
        self.posts = [post("c%d" % i, "cooking", ["spaghetti", "kettlebell"]) for i in range(12)]
        self.posts += [post("d%d" % i, "cooking", ["cake"]) for i in range(4)]
        self.posts += [post("f%d" % i, "fitness", ["protein"]) for i in range(8)]
        self.posts += [post("c_prot%d" % i, "cooking", ["protein"]) for i in range(3)]
        self.by_id = {p["id"]: p for p in self.posts}
        self.tc = tb.tag_category_counts(self.posts)
        self.bad = tb.blacklist(self.tc)
        self.subs = [{"id": "pasta", "title": "Pasta"}, {"id": "desserts", "title": "Desserts"}]

    def test_min_posts_blacklist_and_foreign_category(self):
        assign = {p["id"]: "pasta" for p in self.posts if p["id"].startswith("c")}
        assign.update({p["id"]: "desserts" for p in self.posts if p["id"].startswith("d")})
        kids = tb.build_nodes("cooking", self.subs, assign, self.by_id, self.tc, self.bad)
        self.assertEqual([k["id"] for k in kids], ["pasta"])          # desserts: 4 < 10
        self.assertEqual(kids[0]["posts"], 15)
        self.assertEqual(kids[0]["aliases"], ["spaghetti"])          # no kettlebell, no protein
        tn.validate_tree([{"id": "cooking", "children": [
            {k: v for k, v in c.items() if k != "posts"} for c in kids]}])

    def test_post_text_uses_all_signals(self):
        self.assertEqual(tb.post_text({"caption": "a  b", "transcript": "", "visual": "kitchen"}),
                         "a b | kitchen")


class L1Only(unittest.TestCase):
    TREE = [{"id": "cooking", "title": "Г", "children": [{"id": "pasta", "title": "П",
                                                           "aliases": ["spaghetti"]}]},
            {"id": "other", "title": "Р"}]

    def test_l1_only(self):
        ix = tn.TreeIndex(self.TREE)
        ids = tn.normalize(["other"], ["spaghetti", "kino"], ix, {"kino": 9})
        self.assertEqual(ids, ["other", "cooking", "pasta", "misc_kino"])
        self.assertEqual(pc.l1_only(ids, ix), ["other", "cooking"])

    def test_l1_tree(self):
        self.assertEqual(pc.l1_tree(self.TREE), [{"id": "cooking", "title": "Г"},
                                                 {"id": "other", "title": "Р"}])


if __name__ == "__main__":
    unittest.main()
