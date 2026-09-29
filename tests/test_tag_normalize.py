import sqlite3
import unittest

import tag_normalize as tn

TREE = [
    {"id": "cooking", "title": "Готовка", "children": [
        {"id": "pasta", "title": "Паста", "aliases": ["pasta-recipe", "Spaghetti"]},
        {"id": "baking", "title": "Выпечка", "aliases": ["sourdough"],
         "children": [{"id": "bread", "title": "Хлеб", "aliases": ["baguette"]}]},
    ]},
    {"id": "fitness", "title": "Фитнес", "children": [
        {"id": "kettlebell_training", "title": "Гири", "aliases": ["kettlebell", "deadlift"],
         "only_with_parent": True},
        {"id": "yoga", "title": "Йога"},
    ]},
    {"id": "other", "title": "Разное"},
]


class TreeValidation(unittest.TestCase):
    def test_ok(self):
        tn.validate_tree(TREE)

    def test_bad_id(self):
        with self.assertRaises(ValueError):
            tn.validate_tree([{"id": "Bad-Id"}])

    def test_duplicate(self):
        with self.assertRaises(ValueError):
            tn.validate_tree([{"id": "a", "children": [{"id": "a"}]}])

    def test_depth(self):
        deep = [{"id": "a", "children": [{"id": "b", "children": [
            {"id": "c", "children": [{"id": "d"}]}]}]}]
        with self.assertRaises(ValueError):
            tn.validate_tree(deep)


class Normalize(unittest.TestCase):
    def setUp(self):
        self.ix = tn.TreeIndex(TREE)

    def test_category_first_then_alias(self):
        self.assertEqual(tn.normalize(["cooking"], ["pasta-recipe"], self.ix),
                         ["cooking", "pasta"])

    def test_alias_case_and_separator_insensitive(self):
        self.assertEqual(tn.normalize(["cooking"], ["SPAGHETTI", "pasta_recipe"], self.ix),
                         ["cooking", "pasta"])

    def test_ancestors_added(self):
        self.assertEqual(tn.normalize(["other"], ["baguette"], self.ix),
                         ["other", "cooking", "baking", "bread"])

    def test_id_matches_directly(self):
        self.assertEqual(tn.normalize(["fitness"], ["yoga"], self.ix), ["fitness", "yoga"])

    def test_unknown_category_dropped(self):
        self.assertEqual(tn.normalize(["astrology"], ["yoga"], self.ix), ["fitness", "yoga"])

    def test_misc_threshold(self):
        counts = tn.tag_counts([["kino"]] * 5 + [["rare-thing"]] * 4)
        self.assertEqual(tn.normalize(["other"], ["kino", "rare-thing"], self.ix, counts),
                         ["other", "misc_kino"])

    def test_misc_slug(self):
        counts = {"soy_sauce": 7}
        self.assertEqual(tn.normalize([], ["soy-sauce"], self.ix, counts), ["misc_soy_sauce"])

    def test_only_with_parent_leak_guard(self):
        self.assertEqual(tn.normalize(["comedy", "other"], ["kettlebell", "deadlift"], self.ix),
                         ["other"])
        self.assertEqual(tn.normalize(["fitness"], ["kettlebell", "deadlift"], self.ix),
                         ["fitness", "kettlebell_training"])

    def test_dedup_and_empty(self):
        self.assertEqual(tn.normalize(["cooking", "cooking"], ["", "pasta", "pasta-recipe"],
                                      self.ix), ["cooking", "pasta"])
        self.assertEqual(tn.normalize(None, None, self.ix), [])

    def test_hashtag_line(self):
        self.assertEqual(tn.hashtag_line(["cooking", "pasta"]), "#cooking #pasta")


class RepoTree(unittest.TestCase):
    def test_repo_tags_tree_is_valid(self):
        tree = tn.load_tree()
        ix = tn.TreeIndex(tree)
        self.assertIn("other", ix.level1)
        for n in tree:
            self.assertIsNone(ix.parent[n["id"]])


if __name__ == "__main__":
    unittest.main()
