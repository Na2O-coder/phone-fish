import os
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

import snowluma_relationships as relationships  # noqa: E402


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {}

    def snapshot(self):
        return {"qq": {"selfId": "10000001"}, "userProfiles": {}}


class FakeService:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.state_store = FakeStore()
        self.logs = []

    def log(self, message):
        self.logs.append(message)


OLD_SCHEMA = """
CREATE TABLE relationships (
    owner_qq TEXT NOT NULL, peer_qq TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT NOT NULL,
    familiarity INTEGER NOT NULL DEFAULT 50, trust INTEGER NOT NULL DEFAULT 50,
    tension INTEGER NOT NULL DEFAULT 0, stance TEXT NOT NULL DEFAULT '',
    last_event TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL,
    PRIMARY KEY(owner_qq, peer_qq, scope, scope_id)
)
"""


class GlobalRelationshipTests(unittest.TestCase):
    def test_migrates_scoped_rows_keeping_highest_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            conn = sqlite3.connect(path)
            conn.execute(OLD_SCHEMA)
            rows = [
                ("10000001", "10000002", "group", "100000001", 61, 56, 0, "", "送糖", 1000),
                ("10000001", "10000002", "group", "100000002", 55, 51, 0, "", "玩笑称呼", 2000),
                ("10000001", "10000002", "private", "10000002", 57, 52, 0, "", "私聊", 500),
                ("10000001", "10000003", "group", "100000001", 59, 50, 0, "", "轻松互动", 1500),
            ]
            conn.executemany("INSERT INTO relationships VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            conn.close()

            service = FakeService(tmp)
            store = relationships.RelationshipStore(service)
            store.ensure_schema()

            state = store.state_for("group", "group:100000001", "10000002")
            self.assertEqual(state["familiarity"], 61)
            self.assertEqual(state["trust"], 56)
            self.assertEqual(state["tension"], 0)
            self.assertEqual(store.state_for("private", "private:10000002", "10000002")["familiarity"], 61)

            rows = store.list_states("group", "group:100000001")
            target_rows = [row for row in rows if row["peer_qq"] == "10000002"]
            self.assertEqual(len(target_rows), 1)

            conn = sqlite3.connect(path)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(relationships)").fetchall()]
            self.assertNotIn("scope", columns)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0], 2)
            conn.close()

            store.set_state("group", "group:100000002", "10000002", familiarity=62)
            self.assertEqual(store.state_for("group", "group:100000001", "10000002")["familiarity"], 62)
            self.assertEqual(store.state_for("private", "x", "10000002")["familiarity"], 62)

            store.ensure_schema()
            self.assertEqual(len(store.list_states("group", "group:100000001")), 2)

    def test_global_schema_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "memory.db")
            conn = sqlite3.connect(path)
            conn.execute("""
            CREATE TABLE relationships (
                owner_qq TEXT NOT NULL, peer_qq TEXT NOT NULL,
                familiarity INTEGER NOT NULL DEFAULT 50, trust INTEGER NOT NULL DEFAULT 50,
                tension INTEGER NOT NULL DEFAULT 0, stance TEXT NOT NULL DEFAULT '',
                last_event TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL,
                PRIMARY KEY(owner_qq, peer_qq)
            )
            """)
            conn.execute("INSERT INTO relationships VALUES(?,?,?,?,?,?,?,?)",
                         ("10000001", "10000002", 58, 54, 0, "", "全局", 1000))
            conn.commit()
            conn.close()

            service = FakeService(tmp)
            store = relationships.RelationshipStore(service)
            store.ensure_schema()
            self.assertEqual(store.state_for("group", "group:1", "10000002")["familiarity"], 58)
            self.assertFalse(any("合并为全局" in line for line in service.logs))


if __name__ == "__main__":
    unittest.main()

