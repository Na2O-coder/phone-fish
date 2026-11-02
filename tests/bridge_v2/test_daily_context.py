import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

import snowluma_conversation as conversation_mod  # noqa: E402
import snowluma_service as service_mod  # noqa: E402


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"qq": {"selfId": "10000001"}, "history": {}}

    def snapshot(self):
        return self.data


class FakeConfig:
    def __init__(self, context):
        self.context = context

    def get(self, key=None, default=None):
        config = {
            "timeZone": "Asia/Shanghai",
            "behavior": {"context": self.context},
        }
        if key is None:
            return config
        return config.get(key, default)


class FakeService:
    def __init__(self, state_dir, context):
        self.state_dir = state_dir
        self.state_store = FakeStore()
        self.config_store = FakeConfig(context)
        self.logs = []

    def log(self, message):
        self.logs.append(str(message))


class DailyContextTests(unittest.TestCase):
    def make_service(self, tmp):
        service = FakeService(tmp, {"mode": "daily", "dayStartHour": 7, "maxInputTokens": 900000,
                                    "retentionDays": 2, "replayHistoryImages": True})
        store = conversation_mod.ConversationStore(service)
        store.ensure_schema()
        service.conversation = store
        engine = service_mod.AutoReplyEngine(service)
        service.auto_engine = engine
        return service, store, engine

    def add_turn(self, store, chat_key, user_text, assistant_text, message_id):
        user_id = store.record_user(chat_key, 1793500000000, message_id, "1", "甲", user_text)
        store.set_prompt(user_id, user_text, [])
        if assistant_text:
            store.record_assistant(chat_key, 1793500000001, assistant_text, source="auto")
        return user_id

    def test_daily_prompt_prefix_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store, engine = self.make_service(tmp)
            chat = "group:1"
            system = "SYSTEM"

            u1 = store.record_user(chat, 1793500000000, "m1", "1", "甲", "第一句")
            store.set_prompt(u1, "第一句", [])
            prior1 = engine._build_daily_messages(service.config_store.get(), chat, "group", "1", u1, system)
            self.assertIsNotNone(prior1)
            req1 = prior1 + [{"role": "user", "content": "第一句"}]
            store.record_assistant(chat, 1793500000001, "第一回", source="auto")

            u2 = store.record_user(chat, 1793500000002, "m2", "1", "甲", "第二句")
            store.set_prompt(u2, "第二句", [])
            prior2 = engine._build_daily_messages(service.config_store.get(), chat, "group", "1", u2, system)
            self.assertIsNotNone(prior2)
            req2 = prior2 + [{"role": "user", "content": "第二句"}]
            self.assertEqual(req2[:len(req1)], req1)
            store.record_assistant(chat, 1793500000003, "第二回", source="auto")

            u3 = store.record_user(chat, 1793500000004, "m3", "1", "甲", "第三句")
            store.set_prompt(u3, "第三句", [])
            prior3 = engine._build_daily_messages(service.config_store.get(), chat, "group", "1", u3, system)
            self.assertIsNotNone(prior3)
            req3 = prior3 + [{"role": "user", "content": "第三句"}]
            self.assertEqual(req3[:len(req2)], req2)

    def test_compact_skipped_message_is_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store, engine = self.make_service(tmp)
            chat = "group:1"
            skipped = store.record_user(chat, 1793500000000, "skip1", "2", "乙", "没回的消息")
            current = store.record_user(chat, 1793500000001, "cur1", "1", "甲", "当前")
            messages = engine._build_daily_messages(service.config_store.get(), chat, "group", "1", current, "SYS")
            self.assertIsNotNone(messages)
            self.assertIn("没回的消息", str(messages))
            self.assertNotIn("当前", str(messages))

    def test_record_incoming_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store, engine = self.make_service(tmp)
            event = {"message_type": "group", "group_id": "1", "user_id": "2", "sender_name": "甲",
                     "message_id": "evt1", "text": "你好", "image_types": []}
            turn_id = engine._record_daily_user(event, "group", "1", "2", "group:1")
            self.assertTrue(turn_id)
            self.assertEqual(event.get("_conversation_turn_id"), turn_id)
            rows = store.list_turns("group:1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["role"], "user")
            self.assertEqual(rows[0]["text"], "你好")

    def test_compaction_keeps_recent_turns_and_persists_across_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store, engine = self.make_service(tmp)
            chat = "group:1"
            first = store.record_user(chat, 1793400000000, "old", "1", "甲", "旧消息")
            store.set_prompt(first, "旧消息", [])
            for i in range(100):
                turn = store.record_user(chat, 1793500000000 + i * 2, "m%d" % i, "1", "甲", "消息%d" % i)
                store.set_prompt(turn, "消息%d" % i, [])
                store.record_assistant(chat, 1793500000001 + i * 2, "回复%d" % i, source="auto")
            with service.state_store.lock:
                service.state_store.data["cacheMetrics"] = {chat: {"lastPromptTokens": 700000}}
            messages = engine._build_daily_messages(service.config_store.get(), chat, "group", "1", 0, "SYS")
            self.assertIsNotNone(messages)
            with service.state_store.lock:
                starts = service.state_store.data.get("conversationStartIds") or {}
                self.assertIn(chat, starts)
                self.assertGreater(starts[chat], 1)
                self.assertEqual(service.state_store.data["cacheMetrics"][chat]["lastPromptTokens"], 0)

    def test_reset_clears_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store, engine = self.make_service(tmp)
            chat = "group:1"
            self.add_turn(store, chat, "一", "回", "m1")
            store.clear_chat(chat)
            self.assertEqual(store.list_turns(chat, exclude_id=0), [])


if __name__ == "__main__":
    unittest.main()

