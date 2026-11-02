import json
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

import snowluma_context as context  # noqa: E402
import snowluma_prompts as prompts  # noqa: E402
import snowluma_skills as skills  # noqa: E402


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}

    def snapshot(self):
        return {
            "qq": {"selfId": "10000001", "nickname": "小鲸鱼"},
            "userProfiles": {},
            "groupParticipants": {},
        }


class FakeService:
    def __init__(self):
        self.state_dir = os.path.join(ROOT, "bridge")
        self.state_store = FakeStore()
        self.config_store = type("Config", (), {"get": lambda _self: CONFIG})()

    def profiles_prompt(self, *_args, **_kwargs):
        return ""


_CONFIG_PATH = os.path.join(ROOT, "bridge", "config.json")
if not os.path.exists(_CONFIG_PATH):
    _CONFIG_PATH = os.path.join(ROOT, "bridge", "config.example.json")
CONFIG = json.load(open(_CONFIG_PATH, encoding="utf-8"))


class ContextTests(unittest.TestCase):
    def test_quote_attribution_and_addressed(self):
        event = {
            "message_type": "group",
            "group_id": "100000001",
            "user_id": "10000003",
            "sender_name": "群友A",
            "message_id": "m1",
            "at_qqs": ["10000001", "10000004"],
            "raw": {},
            "_recent_speakers": [{"name": "群友D", "user": "1", "text": "每天看一遍"}],
        }
        quote = {"user_id": "10000004", "nickname": "群友B", "text": "心情不好了继续骂她"}
        env = context.build_envelope(
            FakeService(), event, CONFIG, "别骂小鲸鱼了", quote=quote,
            addressed=True, reply_target="10000004",
            recent_speakers=event["_recent_speakers"])
        rendered = context.render(env)
        self.assertIn("[引用消息｜原发送者] 群友B(10000004)", rendered)
        self.assertIn("[当前消息｜发送者] 群友A(10000003)", rendered)
        self.assertIn("[是否直接对你] 是", rendered)
        self.assertEqual(env["reply_target"], "10000004")

    def test_unaddressed_second_person(self):
        event = {
            "message_type": "group",
            "group_id": "100000001",
            "user_id": "10000002",
            "sender_name": "群友C",
            "message_id": "m2",
            "at_qqs": [],
            "raw": {},
            "_recent_speakers": [{"name": "群友D", "user": "10000005", "text": "每天看一遍感觉整个人都通透了"}],
        }
        env = context.build_envelope(
            FakeService(), event, CONFIG, "你也快了", addressed=False,
            recent_speakers=event["_recent_speakers"])
        rendered = context.render(env)
        self.assertIn("[是否直接对你] 否", rendered)
        self.assertIn("群友D：每天看一遍", rendered)

    def test_quote_unavailable_marker(self):
        event = {
            "message_type": "group",
            "group_id": "1",
            "user_id": "2",
            "sender_name": "A",
            "message_id": "m3",
            "at_qqs": [],
            "raw": {"message": [{"type": "reply", "data": {"id": "x"}}]},
        }
        env = context.build_envelope(FakeService(), event, CONFIG, "看这个", quote=None)
        self.assertFalse(env["quote"]["available"])
        self.assertIn("原内容不可见", context.render(env))

    def test_prompt_bundle_is_single_source_without_stale_tools(self):
        service = FakeService()
        bundle = prompts.load_bundle(service, CONFIG)
        self.assertTrue(bundle["text"])
        self.assertTrue(bundle["version"])
        for stale in ("send_message", "send_sticker", "memory_append", "list_stickers"):
            self.assertNotIn(stale, bundle["text"])

    def test_meme_search_is_explicit_only(self):
        self.assertEqual(skills._meme_candidate("难绷"), "")
        self.assertEqual(skills._meme_candidate("乐乐"), "")
        self.assertTrue(skills._meme_candidate("查一下 寿来"))
        self.assertTrue(skills._meme_candidate("这是什么梗"))

    def test_sticker_candidates_are_model_choice_policy(self):
        bundle = prompts.load_bundle(FakeService(), CONFIG)
        self.assertIn("每 3–5 轮", bundle["text"])
        self.assertNotIn("补一张", bundle["text"])

    def test_self_claim_filter(self):
        self.assertEqual(skills.strip_self_claim("行了行了 我知道是我"), "")
        self.assertEqual(skills.strip_self_claim("行吧\n我知道是我"), "行吧")
        self.assertEqual(skills.strip_self_claim("这是别人"), "这是别人")

    def test_rule_similarity_marks_near_duplicates(self):
        first = "遇到群友用“妈妈”等玩笑称呼或明显玩笑请求时，先顺着梗轻松接一句，再用一句话轻轻划线，不连续严肃纠正。"
        second = "遇到群友E用“妈妈”等玩笑称呼时，先顺着梗轻松接一句并轻轻划线，不连续严肃纠正。"
        unrelated = "被说“别说不吉利”时先收住，不要回怼。"
        self.assertGreaterEqual(skills._rule_similarity(first, second), skills.RULE_SIMILAR_THRESHOLD)
        self.assertLess(skills._rule_similarity(first, unrelated), skills.RULE_SIMILAR_THRESHOLD)
        self.assertEqual(skills._similar_rule_ids(second, [{"id": "r1", "proposal": first},
                                                           {"id": "r2", "proposal": unrelated}]), ["r1"])

    def test_daily_digest_merges_same_person_across_scopes(self):
        import sqlite3
        import tempfile
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE relationships (owner_qq TEXT, peer_qq TEXT, scope TEXT, scope_id TEXT, "
                "familiarity INTEGER, trust INTEGER, tension INTEGER, stance TEXT, last_event TEXT, updated_at INTEGER)")
            base = int(datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
            rows = [
                ("10000001", "10000002", "group", "100000001", 61, 56, 0, "", "送糖", base),
                ("10000001", "10000002", "group", "100000002", 55, 51, 0, "", "玩笑称呼", base + 1000),
                ("10000001", "10000003", "group", "100000001", 59, 50, 0, "", "轻松互动", base + 2000),
            ]
            conn.executemany("INSERT INTO relationships VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            conn.close()

            service = FakeService()
            service.state_dir = tmp
            text = skills.build_daily_digest(service, CONFIG, "2026-10-31")
            self.assertEqual(text.count("10000002"), 1)
            self.assertIn("熟悉 61 / 信任 56", text)
            self.assertEqual(text.count("10000003"), 1)

    def test_dynamic_prompt_is_separate(self):
        service = FakeService()
        static = skills.build_system_prompt(service, CONFIG, "group:100000001", "group", user_id="10000003", include_dynamic=False)
        self.assertNotIn("当前时间", static)

    def test_system_prompt_uses_bundle(self):
        service = FakeService()
        text = skills.build_system_prompt(service, CONFIG, "group:100000001", "group", user_id="10000003")
        self.assertIn("小鲸鱼", text)
        for stale in ("send_message", "send_sticker", "memory_append", "list_stickers"):
            self.assertNotIn(stale, text)


if __name__ == "__main__":
    unittest.main()
