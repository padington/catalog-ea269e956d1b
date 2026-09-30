import json, os, random, tempfile, unittest
from unittest import mock

import bot_search as bs
import vps_service as vs

FX = os.path.join(os.path.dirname(__file__), "fixtures", "index_small.json")


def load():
    return bs.Index(bs.load_file(FX))


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.idx = load()

    def test_depth_limited_to_3(self):
        self.assertEqual(self.idx.depth["carbonara"], 3)
        self.assertNotIn("too_deep", self.idx.nodes)
        self.assertEqual(self.idx.children("carbonara"), [])

    def test_counts_include_descendants(self):
        self.assertEqual(self.idx.count("cooking"), 4)     # 10, 11, 12, 20
        self.assertEqual(self.idx.count("pasta"), 2)
        self.assertEqual(self.idx.count("carbonara"), 1)
        self.assertEqual(self.idx.count("fitness"), 7)
        self.assertEqual(self.idx.count("china"), 0)
        self.assertEqual(self.idx.count("free_tag"), 1)    # tag outside the tree still countable

    def test_posts_newest_first(self):
        ms = [self.idx.posts[i]["m"] for i in self.idx.post_ids("fitness")]
        self.assertEqual(ms, sorted(ms, reverse=True))

    def test_root_view_hides_empty(self):
        text, kb = bs.tree_view(self.idx)
        flat = [b for row in kb for b in row]
        self.assertEqual([b["callback_data"] for b in flat], ["n:cooking", "n:fitness"])  # travel: 0 posts
        self.assertIn("Готовка (4)", flat[0]["text"])
        self.assertIn("в индексе: 11", text)

    def test_level2_and_leaf_buttons(self):
        text, kb = bs.tree_view(self.idx, "cooking")
        flat = [b["callback_data"] for row in kb for b in row]
        self.assertEqual(flat, ["n:pasta", "p:baking:0", "p:cooking:0", "n:"])
        self.assertIn("Готовка", text)
        text, kb = bs.tree_view(self.idx, "pasta")
        flat = [b["callback_data"] for row in kb for b in row]
        self.assertEqual(flat, ["p:carbonara:0", "p:pasta:0", "n:cooking"])
        self.assertIn("Готовка › Паста", text)

    def test_node_with_only_empty_children_opens_posts(self):
        idx = bs.Index({"tree": [{"id": "a", "title": "A", "children": [{"id": "b", "title": "B"}]}],
                        "posts": [{"m": 1, "t": ["a"]}]})
        self.assertEqual(bs.tree_view(idx)[1], [[bs.btn("A (1)", "p:a:0")]])
        _, kb, _ = bs.posts_nav(idx, idx.post_ids("a"), 0, "p", "a", "A")
        self.assertEqual(kb, [[bs.btn("⬆️ вверх", "n:")]])

    def test_callback_data_fits(self):
        for nid in list(self.idx.nodes) + [None]:
            for row in bs.tree_view(self.idx, nid)[1]:
                for b in row:
                    self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_real_tree_file(self):
        try:
            import yaml  # noqa: F401  (Mac venv has it; bare python may not)
        except ImportError:
            self.skipTest("no yaml")
        import tag_normalize as tn
        tree = tn.load_tree(tn.DEFAULT_TREE)
        idx = bs.Index({"tree": tree, "posts": [{"m": 1, "t": [n["id"]]} for n in tree]})
        text, kb = bs.tree_view(idx)
        self.assertEqual(sum(len(r) for r in kb), len(tree))


class PagesTest(unittest.TestCase):
    def setUp(self):
        self.idx = load()

    def test_page_of_clamps(self):
        ids = list(range(12))
        self.assertEqual(bs.page_of(ids, 0), ([0, 1, 2, 3, 4], 0, 3))
        self.assertEqual(bs.page_of(ids, 2), ([10, 11], 2, 3))
        self.assertEqual(bs.page_of(ids, 9)[1], 2)
        self.assertEqual(bs.page_of([], 0), ([], 0, 1))

    def test_nav_buttons(self):
        ids = self.idx.post_ids("fitness")             # 7 posts -> 2 pages
        text, kb, mids = bs.posts_nav(self.idx, ids, 0, "p", "fitness", "Фитнес")
        self.assertEqual(mids, [19, 18, 17, 16, 15])
        self.assertEqual(kb, [[bs.btn("дальше ▶️", "p:fitness:1")], [bs.btn("⬆️ вверх", "n:fitness")]])
        self.assertIn("1–5 из 7", text)
        text, kb, mids = bs.posts_nav(self.idx, ids, 1, "p", "fitness", "Фитнес")
        self.assertEqual(mids, [14, 13])
        self.assertEqual(kb[0], [bs.btn("◀️ назад", "p:fitness:0")])
        self.assertIn("6–7 из 7", text)

    def test_nav_up_from_leaf_goes_to_parent(self):
        _, kb, _ = bs.posts_nav(self.idx, self.idx.post_ids("baking"), 0, "p", "baking", "x")
        self.assertEqual(kb, [[bs.btn("⬆️ вверх", "n:cooking")]])
        _, kb, _ = bs.posts_nav(self.idx, self.idx.post_ids("free_tag"), 0, "p", "free_tag", "x")
        self.assertEqual(kb, [[bs.btn("⬆️ вверх", "n:")]])

    def test_find_nav_has_no_up(self):
        text, kb, mids = bs.posts_nav(self.idx, [], 0, "f", None, "🔎 «x»")
        self.assertEqual((kb, mids), ([], []))
        self.assertIn("ничего", text)

    def test_parse_callback(self):
        self.assertEqual(bs.parse_callback("n:"), ("n", None, 0))
        self.assertEqual(bs.parse_callback("n:pasta"), ("n", "pasta", 0))
        self.assertEqual(bs.parse_callback("p:pasta:2"), ("p", "pasta", 2))
        self.assertEqual(bs.parse_callback("f:3"), ("f", None, 3))
        self.assertEqual(bs.parse_callback("r:"), ("r", None, 0))
        self.assertEqual(bs.parse_callback("p:x:y"), (None, None, 0))
        self.assertEqual(bs.parse_callback("zzz"), (None, None, 0))


class FindTest(unittest.TestCase):
    def setUp(self):
        self.idx = load()

    def ms(self, ids):
        return [self.idx.posts[i]["m"] for i in ids]

    def test_all_words_case_insensitive(self):
        self.assertEqual(self.ms(self.idx.find("КАРБОНАРА пекорино")), [10])

    def test_tags_and_titles(self):
        self.assertEqual(self.ms(self.idx.find("#pasta")), [11, 10])
        self.assertEqual(self.ms(self.idx.find("паста")), [11, 10])          # title "Паста" + text
        self.assertEqual(sorted(self.ms(self.idx.find("гири"))), [13, 14, 16, 17, 18, 19])

    def test_yo_and_author(self):
        self.assertEqual(self.ms(self.idx.find("елочные")), [20])
        self.assertEqual(self.ms(self.idx.find("nonna")), [11])

    def test_fallback_any_word_ranked(self):
        self.assertEqual(self.ms(self.idx.find("закваске марсианин")), [12])
        self.assertEqual(self.idx.find("марсианин"), [])
        self.assertEqual(self.idx.find("  "), [])

    def test_resolve_tag(self):
        self.assertEqual(self.idx.resolve_tag("#Pasta"), "pasta")
        self.assertEqual(self.idx.resolve_tag("Гири"), "kettlebell")
        self.assertEqual(self.idx.resolve_tag("макароны"), "pasta")
        self.assertEqual(self.idx.resolve_tag("free_tag"), "free_tag")
        self.assertIsNone(self.idx.resolve_tag("nope"))

    def test_random(self):
        rng = random.Random(1)
        for _ in range(20):
            i = self.idx.random_post("pasta", rng)
            self.assertIn(self.idx.posts[i]["m"], (10, 11))
        self.assertIsNone(self.idx.random_post("china", rng))
        self.assertIsNotNone(self.idx.random_post(None, rng))


class StoreTest(unittest.TestCase):
    def test_file_mode_and_mtime_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "index.json")
            st = bs.IndexStore(p, logf=lambda m: None)
            ok, msg = st.reload()
            self.assertFalse(ok)
            self.assertIn("FileNotFoundError", st.error)
            with open(FX) as f, open(p, "w") as g:
                g.write(f.read())
            st.loaded_at = 0
            st.maybe_reload()
            self.assertEqual(len(st.idx.posts), 11)
            self.assertEqual(st.error, "")

    def test_github_failure_falls_back_to_cache(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "index.json")
            with open(FX) as f, open(p, "w") as g:
                g.write(f.read())
            st = bs.IndexStore(p, repo="o/r", token="t", logf=lambda m: None)
            with mock.patch.object(bs, "fetch_index_github", side_effect=OSError("boom")):
                ok, msg = st.reload()
            self.assertTrue(ok)
            self.assertIn("github недоступен", msg)

    def test_github_ok_writes_cache(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "index.json")
            st = bs.IndexStore(p, repo="o/r", token="t", logf=lambda m: None)
            with mock.patch.object(bs, "fetch_index_github", return_value=bs.load_file(FX)):
                ok, _ = st.reload()
            self.assertTrue(ok)
            self.assertEqual(len(bs.load_file(p)["posts"]), 11)


class ServiceSearchTest(unittest.TestCase):
    """Service wiring with Telegram mocked: what goes out for /tags, a callback, /find, /random."""

    def make(self, owners=("7",)):
        st = bs.IndexStore(FX, logf=lambda m: None)
        st.reload()
        with mock.patch.object(vs, "load_json", side_effect=lambda p, d: d):
            return vs.Service(None, "-100", set(owners), store=st)

    def run_msg(self, svc, text, uid="7"):
        calls = []
        with mock.patch.object(vs, "tg", side_effect=lambda m, d=None, **k: calls.append((m, d)) or {"ok": True}):
            svc.handle_message({"text": text, "from": {"id": int(uid)}, "chat": {"id": 7, "type": "private"},
                                "message_id": 1})
        return calls

    def run_cb(self, svc, data, uid="7"):
        calls = []
        with mock.patch.object(vs, "tg", side_effect=lambda m, d=None, **k: calls.append((m, d)) or {"ok": True}):
            svc.handle_callback({"id": "cb1", "from": {"id": int(uid)}, "data": data,
                                 "message": {"message_id": 50, "chat": {"id": 7}, "text": "old"}})
        return calls

    def test_tags_sends_keyboard(self):
        calls = self.run_msg(self.make(), "/tags")
        self.assertEqual(calls[0][0], "sendMessage")
        kb = json.loads(calls[0][1]["reply_markup"])["inline_keyboard"]
        self.assertEqual(kb[0][0]["callback_data"], "n:cooking")

    def test_callback_node_edits_and_answers(self):
        calls = self.run_cb(self.make(), "n:cooking")
        self.assertEqual([c[0] for c in calls], ["answerCallbackQuery", "editMessageText"])
        self.assertNotIn("text", calls[0][1])

    def test_callback_posts_copies_5_then_nav(self):
        calls = self.run_cb(self.make(), "p:fitness:0")
        methods = [c[0] for c in calls]
        self.assertEqual(methods, ["answerCallbackQuery", "editMessageText"] + ["copyMessage"] * 5 + ["sendMessage"])
        self.assertEqual([c[1]["message_id"] for c in calls if c[0] == "copyMessage"], [19, 18, 17, 16, 15])
        self.assertEqual(calls[2][1]["from_chat_id"], "-100")

    def test_find_and_page(self):
        svc = self.make()
        calls = self.run_msg(svc, "/find гири")
        self.assertEqual([c[0] for c in calls].count("copyMessage"), 5)
        calls = self.run_cb(svc, "f:1")
        self.assertEqual([c[1]["message_id"] for c in calls if c[0] == "copyMessage"], [13])

    def test_random_tag(self):
        calls = self.run_msg(self.make(), "/random Паста")
        cp = [c[1]["message_id"] for c in calls if c[0] == "copyMessage"]
        self.assertEqual(len(cp), 1)
        self.assertIn(cp[0], (10, 11))
        calls = self.run_msg(self.make(), "/random nope")
        self.assertIn("Не знаю тег", calls[0][1]["text"])

    def test_owner_only(self):
        calls = self.run_msg(self.make(), "/tags", uid="8")           # not an owner: message ignored
        self.assertEqual(calls, [])
        calls = self.run_cb(self.make(), "p:fitness:0", uid="8")
        self.assertEqual([c[0] for c in calls], ["answerCallbackQuery"])
        self.assertEqual(calls[0][1]["show_alert"], "true")
        calls = self.run_msg(self.make(owners=()), "/tags")             # OWNER_IDS empty: no search
        self.assertEqual(len(calls), 1)
        self.assertIn("только владельцу", calls[0][1]["text"])
        self.assertNotIn("reply_markup", calls[0][1])

    def test_links_path_untouched(self):
        svc = self.make()
        with mock.patch.object(vs, "save_json"):
            calls = self.run_msg(svc, "https://www.instagram.com/reel/DQbVZ2aEdZl/")
        self.assertEqual(calls, [])
        self.assertEqual(svc.queue[0]["code"], "DQbVZ2aEdZl")

    def test_command_arg(self):
        self.assertEqual(vs.command_arg("/find@libinstabot паста  карбонара "), "паста  карбонара")
        self.assertEqual(vs.command_arg("/random"), "")


if __name__ == "__main__":
    unittest.main()
