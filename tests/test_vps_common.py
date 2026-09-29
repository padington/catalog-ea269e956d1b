import json, os, tempfile, unittest
from unittest import mock

import vps_common as vc
import vps_service as vs


def cand(w, h, url=None):
    return {"width": w, "height": h, "url": url or "https://cdn/%dx%d.jpg" % (w, h)}


ITEM = {"code": "DQbVZ2aEdZl", "pk": "3904798944555495584", "taken_at": 1759104000,
        "user": {"username": "chef"}, "caption": {"text": "Паста за 10 минут"}}


class LinksTest(unittest.TestCase):
    def test_kinds_and_dedup(self):
        text = ("смотри https://www.instagram.com/reel/DQbVZ2aEdZl/?igsh=abc и "
                "instagram.com/p/Cxyz_-12345/ и https://instagram.com/reels/DQbVZ2aEdZl/ "
                "https://www.instagram.com/tv/B1LbfVPlwIA https://www.instagram.com/chef/reel/C0dE12345ab/")
        self.assertEqual(vc.parse_links(text), ["DQbVZ2aEdZl", "Cxyz_-12345", "B1LbfVPlwIA", "C0dE12345ab"])

    def test_no_links(self):
        self.assertEqual(vc.parse_links("привет https://youtube.com/watch?v=1"), [])
        self.assertEqual(vc.parse_links(None), [])

    def test_share_links(self):
        t = "https://www.instagram.com/share/reel/BAabc123/ and https://instagram.com/share/_xYz"
        self.assertEqual(len(vc.parse_share_links(t)), 2)
        self.assertEqual(vc.parse_links(t), [])  # share ids are not media codes; resolved over HTTP

    def test_pk_from_code_matches_instagrapi_examples(self):
        self.assertEqual(vc.pk_from_code("B1LbfVPlwIA"), "2110901750722920960")
        self.assertEqual(vc.pk_from_code("B-fKL9qpeab"), "2278584739065882267")
        self.assertEqual(vc.pk_from_code("CCQQsCXjOaBfS3I2PpqsNkxElV9DXj61vzo5xs0"), "2346448800803776129")
        self.assertEqual(vc.code_from_pk("2110901750722920960"), "B1LbfVPlwIA")
        self.assertRaises(ValueError, vc.pk_from_code, "bad!code")

    def test_pk_from_code_same_as_instagrapi(self):
        try:
            from instagrapi import Client
        except Exception:
            self.skipTest("instagrapi not installed")
        cl = Client()
        for code in ("DQbVZ2aEdZl", "C0dE12345ab", "B-fKL9qpeab"):
            self.assertEqual(vc.pk_from_code(code), str(cl.media_pk_from_code(code)))


class CaptionTest(unittest.TestCase):
    def test_layout(self):
        c = vc.caption_for(ITEM, shared_by="friend")
        lines = c.split("\n")
        self.assertEqual(lines[0], "Паста за 10 минут")
        self.assertEqual(lines[2], "https://www.instagram.com/reel/DQbVZ2aEdZl/")
        self.assertEqual(lines[3], "@chef · 2025-09-29 · from friend")
        self.assertEqual(lines[-1], "#nonparsed")

    def test_limit_and_cut(self):
        item = dict(ITEM, caption={"text": "😀" * 2000})   # emoji = 2 UTF-16 units
        c = vc.caption_for(item)
        self.assertLessEqual(vc.utf16_len(c), 1024)
        self.assertGreater(vc.utf16_len(c), 1000)
        self.assertTrue(c.endswith("\n#nonparsed"))
        self.assertIn("…\n\nhttps://www.instagram.com/reel/", c)
        item = dict(ITEM, caption={"text": "a" * 5000})
        c = vc.caption_for(item)
        self.assertEqual(vc.utf16_len(c), 1024)

    def test_short_caption_not_cut(self):
        self.assertNotIn("…", vc.caption_for(ITEM))

    def test_no_caption(self):
        c = vc.caption_for(dict(ITEM, caption=None))
        self.assertTrue(c.startswith("https://www.instagram.com/reel/DQbVZ2aEdZl/"))

    def test_unavailable_from_fallback(self):
        c = vc.caption_for({}, "friend", vc.UNAVAILABLE,
                           {"pk": "2110901750722920960", "caption": "из DM"}, prefix="⚠️ unavailable")
        self.assertEqual(c.split("\n"), ["⚠️ unavailable", "", "из DM", "",
                                         "https://www.instagram.com/reel/B1LbfVPlwIA/", "from friend", "#unavailable"])

    def test_custom_tag_last_line(self):
        self.assertEqual(vc.caption_for(ITEM, tag="#cooking #pasta").split("\n")[-1], "#cooking #pasta")


class MediaPickTest(unittest.TestCase):
    def test_thumb_largest_within_320(self):
        item = {"image_versions2": {"candidates": [cand(1080, 1920), cand(320, 568), cand(240, 426), cand(180, 320),
                                                   cand(320, 320), cand(150, 150)]}}
        self.assertEqual(vc.pick_thumb(item), "https://cdn/320x320.jpg")
        self.assertIsNone(vc.pick_thumb({"image_versions2": {"candidates": [cand(1080, 1920)]}}))
        self.assertIsNone(vc.pick_thumb({}))

    def test_video_plain(self):
        item = dict(ITEM, video_versions=[{"url": "v1", "width": 720, "height": 1280}], video_duration=12.6)
        view, v = vc.pick_video(item)
        self.assertIs(view, item)
        self.assertEqual(vc.video_params(view, v), {"duration": 13, "width": 720, "height": 1280})

    def test_video_from_carousel_first_video_slide(self):
        item = dict(ITEM, carousel_media=[
            {"image_versions2": {"candidates": [cand(1080, 1080)]}},
            {"video_versions": [{"url": "v2"}], "video_duration": 5.0, "original_width": 1080,
             "original_height": 1350, "image_versions2": {"candidates": [cand(256, 320, "t2")]}},
            {"video_versions": [{"url": "v3"}]}])
        view, v = vc.pick_video(item)
        self.assertEqual(v["url"], "v2")
        self.assertEqual(view["code"], "DQbVZ2aEdZl")
        self.assertEqual(vc.pick_thumb(view), "t2")
        self.assertEqual(vc.video_params(view, v), {"duration": 5, "width": 1080, "height": 1350})

    def test_photo_carousel(self):
        item = dict(ITEM, carousel_media=[{"image_versions2": {"candidates": [cand(640, 640, "s%d" % i),
                                                                             cand(1080, 1080, "b%d" % i)]}}
                                          for i in range(12)])
        view, v = vc.pick_video(item)
        self.assertIsNone(v)
        self.assertEqual(vc.pick_photos(item), ["b%d" % i for i in range(10)])

    def test_single_photo(self):
        self.assertEqual(vc.pick_photos({"image_versions2": {"candidates": [cand(1080, 1350, "x")]}}), ["x"])


class ErrorsTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(vc.classify_error("LoginRequired: login_required"), "login")
        self.assertEqual(vc.classify_error("ChallengeRequired: challenge_required"), "login")
        self.assertEqual(vc.classify_error("PleaseWaitFewMinutes: Please wait a few minutes"), "throttled")
        self.assertEqual(vc.classify_error("ClientError: 429 Too Many Requests"), "throttled")
        self.assertEqual(vc.classify_error("MediaNotFound: Media not found or unavailable"), "not_found")
        self.assertIsNone(vc.classify_error("KeyError: 'url'"))

    def test_channel_link(self):
        self.assertEqual(vc.channel_link("-1004300487255", 42), "https://t.me/c/4300487255/42")


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def private_request(self, path, **kw):
        if self.exc:
            raise self.exc
        return self.resp


class PostMediaTest(unittest.TestCase):
    """post_media with Telegram and CDN mocked: which API method and payload is used."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []

    def fake_tg(self, method, data=None, files=None, **kw):
        self.calls.append((method, dict(data or {}), dict(files or {})))
        if method == "sendMediaGroup":
            n = len(json.loads(data["media"]))
            return {"ok": True, "result": [{"message_id": 100 + i, "photo": [{"file_id": "f%d" % i}]} for i in range(n)]}
        return {"ok": True, "result": {"message_id": 7, "video": {"file_id": "vid"}, "photo": [{"file_id": "ph"}]}}

    def fake_fetch(self, url, path, max_bytes=None):
        with open(path, "wb") as f:
            f.write(b"x" * 10)
        return 10

    def run_post(self, cl, **kw):
        with mock.patch.object(vc, "tg", self.fake_tg), mock.patch.object(vc, "fetch", self.fake_fetch):
            rec = vc.post_media(cl, "3904798944555495584", chat="-100", work=self.tmp, **kw)
        self.assertEqual(os.listdir(self.tmp), [])   # nothing left on disk
        return rec

    def test_video(self):
        item = dict(ITEM, video_versions=[{"url": "v", "width": 720, "height": 1280}], video_duration=9,
                    image_versions2={"candidates": [cand(1080, 1920), cand(180, 320)]})
        rec = self.run_post(FakeClient({"items": [item]}), shared_by="friend")
        method, data, files = self.calls[0]
        self.assertEqual(method, "sendVideo")
        self.assertEqual((data["width"], data["height"], data["duration"]), (720, 1280, 9))
        self.assertIn("thumbnail", files)
        self.assertTrue(data["caption"].endswith("from friend\n#nonparsed"))
        self.assertEqual((rec["status"], rec["kind"], rec["message_id"], rec["file_id"]), ("sent", "video", 7, "vid"))

    def test_photo_album(self):
        item = dict(ITEM, carousel_media=[{"image_versions2": {"candidates": [cand(1080, 1080, "u%d" % i)]}}
                                          for i in range(3)])
        rec = self.run_post(FakeClient({"items": [item]}))
        method, data, _ = self.calls[0]
        self.assertEqual(method, "sendMediaGroup")
        media = json.loads(data["media"])
        self.assertEqual([m["media"] for m in media], ["u0", "u1", "u2"])
        self.assertTrue(media[0]["caption"].endswith("#nonparsed"))
        self.assertNotIn("caption", media[1])
        self.assertEqual((rec["status"], rec["kind"], rec["message_id"], rec["n"]), ("sent", "photos", 100, 3))

    def test_single_photo_uses_sendphoto(self):
        item = dict(ITEM, image_versions2={"candidates": [cand(1080, 1350, "one")]})
        rec = self.run_post(FakeClient({"items": [item]}))
        self.assertEqual(self.calls[0][0], "sendPhoto")
        self.assertEqual(self.calls[0][1]["photo"], "one")
        self.assertEqual(rec["kind"], "photos")

    def test_photo_url_rejected_falls_back_to_upload(self):
        item = dict(ITEM, carousel_media=[{"image_versions2": {"candidates": [cand(1080, 1080, "u%d" % i)]}}
                                          for i in range(2)])
        orig = self.fake_tg

        def tg(method, data=None, files=None, **kw):
            if method == "sendMediaGroup" and not files:
                self.calls.append((method, data, {}))
                return {"ok": False, "description": "Bad Request: failed to get HTTP URL content"}
            return orig(method, data, files, **kw)
        with mock.patch.object(vc, "tg", tg), mock.patch.object(vc, "fetch", self.fake_fetch):
            rec = vc.post_media(FakeClient({"items": [item]}), "1", chat="-100", work=self.tmp)
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertEqual(sorted(self.calls[1][2]), ["p0", "p1"])
        self.assertEqual((rec["status"], rec["via"]), ("sent", "upload"))

    def test_unavailable_empty_items(self):
        rec = self.run_post(FakeClient({"items": []}), fallback={"shortcode": "DQbVZ2aEdZl", "caption": "dm"})
        method, data, _ = self.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertTrue(data["text"].startswith("⚠️ unavailable"))
        self.assertTrue(data["text"].endswith("\n#unavailable"))
        self.assertEqual((rec["status"], rec["kind"], rec["message_id"]), ("unavailable", "text", 7))

    def test_unavailable_not_found_exception(self):
        rec = self.run_post(FakeClient(exc=Exception("Media not found or unavailable")))
        self.assertEqual(rec["status"], "unavailable")

    def test_throttled_and_login(self):
        rec = self.run_post(FakeClient(exc=Exception("Please wait a few minutes before you try again")))
        self.assertEqual((rec["status"], rec["reason"]), ("throttled", "throttled"))
        rec = self.run_post(FakeClient(exc=Exception("login_required")))
        self.assertEqual((rec["status"], rec["reason"]), ("throttled", "login"))
        self.assertEqual(self.calls, [])

    def test_tg_error_is_failed(self):
        item = dict(ITEM, video_versions=[{"url": "v"}])
        with mock.patch.object(vc, "tg", lambda *a, **k: {"ok": False, "description": "Bad Request: x"}), \
                mock.patch.object(vc, "fetch", self.fake_fetch):
            rec = vc.post_media(FakeClient({"items": [item]}), "1", chat="-100", work=self.tmp)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("Bad Request", rec["error"])


class ServiceHelpersTest(unittest.TestCase):
    def test_owners(self):
        self.assertEqual(vs.parse_owner_ids(""), set())
        self.assertEqual(vs.parse_owner_ids(" 1, 2 ,"), {"1", "2"})
        self.assertTrue(vs.is_allowed("5", set()))
        self.assertFalse(vs.is_allowed("5", {"1"}))
        self.assertTrue(vs.is_allowed(1, {"1"}))

    def test_command_of(self):
        self.assertEqual(vs.command_of("/status"), "status")
        self.assertEqual(vs.command_of("/Help@libinstabot x"), "help")
        self.assertIsNone(vs.command_of("https://instagram.com/reel/x"))
        self.assertIsNone(vs.command_of("/"))


if __name__ == "__main__":
    unittest.main()
