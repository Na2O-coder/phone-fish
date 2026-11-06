"""Model-maintained global relationship and episode state.

Relationships are stored once per person (owner + peer), not per chat.  The old
bridge kept one row per group/private context, which made the same person show
up with several different scores; the daily summary mixed them and the reply
prompt could use different values in different chats.  This store migrates the
old scoped rows to one global row, keeping the highest familiarity/trust and
the highest tension ever observed, so a strong bond is not lost during the
merge.  New updates from every chat go to the same global row.
"""
import json
import sqlite3
import threading
import time

GLOBAL_SCOPE = "global"
GLOBAL_SCOPE_ID = "global"


def _text(value):
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _clamp(value, low, high, default=0):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class RelationshipStore:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()

    def _path(self):
        return __import__("os").path.join(getattr(self.service, "state_dir", "/opt/snowluma/bridge"), "memory.db")

    def _connect(self):
        conn = sqlite3.connect(self._path(), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        return conn

    def ensure_schema(self):
        conn = self._connect()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS people (
                qq TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]', first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS relationships (
                owner_qq TEXT NOT NULL, peer_qq TEXT NOT NULL,
                familiarity INTEGER NOT NULL DEFAULT 50, trust INTEGER NOT NULL DEFAULT 50,
                tension INTEGER NOT NULL DEFAULT 0, stance TEXT NOT NULL DEFAULT '',
                last_event TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL,
                PRIMARY KEY(owner_qq, peer_qq)
            );
            CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL, qq TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
                first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                defended_count INTEGER NOT NULL DEFAULT 0, conflict_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(group_id, qq)
            );
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_key TEXT NOT NULL, at INTEGER NOT NULL,
                participants_json TEXT NOT NULL DEFAULT '[]', summary TEXT NOT NULL,
                emotional_valence INTEGER NOT NULL DEFAULT 0, unresolved INTEGER NOT NULL DEFAULT 0,
                source_message_ids TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_chat ON episodes(chat_key, at);
            CREATE TABLE IF NOT EXISTS relationship_meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            self._migrate_to_global(conn)
            conn.commit()
        finally:
            conn.close()

    def _columns(self, conn, table):
        try:
            return [dict(row).get("name", "") for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
        except Exception:
            return []

    def _migrate_to_global(self, conn):
        columns = self._columns(conn, "relationships")
        if columns and "scope" not in columns:
            return
        if not columns:
            return
        try:
            rows = [dict(row) for row in conn.execute("SELECT * FROM relationships").fetchall()]
        except Exception:
            rows = []
        grouped = {}
        for row in rows:
            owner = _text(row.get("owner_qq")).strip()
            peer = _text(row.get("peer_qq")).strip()
            if not owner or not peer:
                continue
            updated_at = int(row.get("updated_at") or 0)
            last_event = _text(row.get("last_event"))[:120]
            stance = _text(row.get("stance"))[:40]
            current = grouped.get((owner, peer))
            if current is None:
                current = {
                    "owner_qq": owner,
                    "peer_qq": peer,
                    "familiarity": _clamp(row.get("familiarity"), 0, 100, 50),
                    "trust": _clamp(row.get("trust"), 0, 100, 50),
                    "tension": _clamp(row.get("tension"), 0, 100, 0),
                    "stance": stance,
                    "last_event": last_event,
                    "updated_at": updated_at,
                    "_event_at": updated_at if last_event else 0,
                    "_stance_at": updated_at if stance else 0,
                }
                grouped[(owner, peer)] = current
                continue
            current["familiarity"] = max(current["familiarity"], _clamp(row.get("familiarity"), 0, 100, 50))
            current["trust"] = max(current["trust"], _clamp(row.get("trust"), 0, 100, 50))
            current["tension"] = max(current["tension"], _clamp(row.get("tension"), 0, 100, 0))
            current["updated_at"] = max(current["updated_at"], updated_at)
            if last_event and updated_at >= current["_event_at"]:
                current["last_event"] = last_event
                current["_event_at"] = updated_at
            if stance and updated_at >= current["_stance_at"]:
                current["stance"] = stance
                current["_stance_at"] = updated_at
        with conn:
            conn.execute("DROP TABLE IF EXISTS relationships_global")
            conn.execute("""
            CREATE TABLE relationships_global (
                owner_qq TEXT NOT NULL, peer_qq TEXT NOT NULL,
                familiarity INTEGER NOT NULL DEFAULT 50, trust INTEGER NOT NULL DEFAULT 50,
                tension INTEGER NOT NULL DEFAULT 0, stance TEXT NOT NULL DEFAULT '',
                last_event TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL,
                PRIMARY KEY(owner_qq, peer_qq)
            )
            """)
            for item in grouped.values():
                conn.execute(
                    "INSERT INTO relationships_global(owner_qq, peer_qq, familiarity, trust, tension, stance, last_event, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (item["owner_qq"], item["peer_qq"], item["familiarity"], item["trust"], item["tension"],
                     item["stance"], item["last_event"], item["updated_at"]))
            conn.execute("DROP TABLE relationships")
            conn.execute("ALTER TABLE relationships_global RENAME TO relationships")
        try:
            self.service.log("关系库已合并为全局：%d 人（取各场景最高值）" % len(grouped))
        except Exception:
            pass

    def _self_qq(self):
        try:
            return _text((self.service.state_store.snapshot().get("qq") or {}).get("selfId"))
        except Exception:
            return ""

    def state_for(self, scene, chat_key, user_id):
        """Global relationship state for one person, shared by every chat."""
        self_qq = self._self_qq()
        if not self_qq or not user_id:
            return {"familiarity": 50, "trust": 50, "tension": 0, "known": False}
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT familiarity, trust, tension FROM relationships "
                    "WHERE owner_qq=? AND peer_qq=?",
                    (self_qq, _text(user_id))).fetchone()
            finally:
                conn.close()
            if not row:
                return {"familiarity": 50, "trust": 50, "tension": 0, "known": False}
            return {"familiarity": _clamp(row["familiarity"], 0, 100, 50),
                    "trust": _clamp(row["trust"], 0, 100, 50),
                    "tension": _clamp(row["tension"], 0, 100, 0), "known": True}
        except Exception:
            return {"familiarity": 50, "trust": 50, "tension": 0, "known": False}

    def list_states(self, scene=None, chat_key=None):
        self_qq = self._self_qq()
        if not self_qq:
            return []
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT peer_qq, familiarity, trust, tension, stance, last_event, updated_at "
                    "FROM relationships WHERE owner_qq=? ORDER BY updated_at DESC LIMIT 50",
                    (self_qq,)).fetchall()
            finally:
                conn.close()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def set_state(self, scene, chat_key, user_id, familiarity=None, trust=None, tension=None):
        self_qq = self._self_qq()
        if not self_qq or not user_id:
            return
        current = self.state_for(scene, chat_key, user_id)
        fam = _clamp(familiarity if familiarity is not None else current["familiarity"], 0, 100, 50)
        tru = _clamp(trust if trust is not None else current["trust"], 0, 100, 50)
        ten = _clamp(tension if tension is not None else current["tension"], 0, 100, 0)
        now = int(time.time() * 1000)
        try:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO relationships(owner_qq, peer_qq, familiarity, trust, tension, stance, last_event, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(owner_qq, peer_qq) DO UPDATE SET "
                        "familiarity=excluded.familiarity, trust=excluded.trust, tension=excluded.tension, updated_at=excluded.updated_at",
                        (self_qq, _text(user_id), fam, tru, ten, "", "", now))
            finally:
                conn.close()
        except Exception as error:
            try:
                self.service.note_storage_error(error, "relationships.db")
            except Exception:
                pass

    def prompt_for(self, scene, chat_key, user_id):
        self_qq = self._self_qq()
        if not self_qq or not user_id:
            return ""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM relationships WHERE owner_qq=? AND peer_qq=?",
                               (self_qq, _text(user_id))).fetchone()
            episodes = conn.execute("SELECT summary, at FROM episodes WHERE chat_key=? ORDER BY at DESC LIMIT 3",
                                    (chat_key,)).fetchall()
        finally:
            conn.close()
        lines = []
        if row:
            familiarity = _clamp(row["familiarity"], 0, 100, 50)
            trust = _clamp(row["trust"], 0, 100, 50)
            tension = _clamp(row["tension"], 0, 100, 0)
            if familiarity >= 70:
                fam = "很熟"
            elif familiarity >= 45:
                fam = "正常熟悉"
            else:
                fam = "不熟"
            if tension >= 65:
                mood = "现在有冲突/尴尬，先别贴太近"
            elif tension >= 35:
                mood = "有点别扭"
            elif trust >= 65:
                mood = "信任，愿意给台阶"
            else:
                mood = ""
            line = "- 当前对话对象：%s；全局关系：%s" % (_text(row["peer_qq"]), fam)
            if mood:
                line += "；%s" % mood
            if _text(row["last_event"]):
                line += "；最近：%s" % _text(row["last_event"])[:80]
            lines.append(line)
        for episode in episodes:
            if _text(episode["summary"]):
                lines.append("- 最近事件：%s" % _text(episode["summary"])[:100])
        if not lines:
            return ""
        return "【关系与最近事件（自然参考，不要报分数）】\n" + "\n".join(lines[:4])

    def _upsert_relationship(self, peer, scope, scope_id, data, now):
        self_qq = self._self_qq()
        if not self_qq or not peer:
            return
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM relationships WHERE owner_qq=? AND peer_qq=?",
                               (self_qq, peer)).fetchone()
            base = dict(row) if row else {"familiarity": 50, "trust": 50, "tension": 0, "stance": ""}
            familiarity = _clamp(base.get("familiarity", 50) + _clamp(data.get("familiarity_delta"), -10, 10, 0), 0, 100, 50)
            trust = _clamp(base.get("trust", 50) + _clamp(data.get("trust_delta"), -10, 10, 0), 0, 100, 50)
            tension = _clamp(base.get("tension", 0) + _clamp(data.get("tension_delta"), -15, 15, 0), 0, 100, 0)
            reason = _text(data.get("reason"))[:120]
            stance = _text(data.get("stance"))[:40]
            with conn:
                conn.execute("""INSERT INTO relationships(owner_qq, peer_qq, familiarity, trust,
                                tension, stance, last_event, updated_at) VALUES(?,?,?,?,?,?,?,?)
                                ON CONFLICT(owner_qq, peer_qq) DO UPDATE SET
                                familiarity=excluded.familiarity, trust=excluded.trust, tension=excluded.tension,
                                stance=excluded.stance, last_event=excluded.last_event, updated_at=excluded.updated_at""",
                             (self_qq, peer, familiarity, trust, tension, stance,
                              reason or _text(base.get("last_event")), now))
        finally:
            conn.close()

    def apply(self, chat_key, scene, user_id, data):
        if not isinstance(data, dict):
            return
        now = int(time.time() * 1000)
        sender = _text(user_id)
        if sender:
            self._upsert_relationship(sender, GLOBAL_SCOPE, GLOBAL_SCOPE_ID, data, now)
        deltas = data.get("relationship_deltas")
        if isinstance(deltas, list):
            for delta in deltas[:8]:
                if isinstance(delta, dict):
                    self._upsert_relationship(_text(delta.get("qq")), GLOBAL_SCOPE, GLOBAL_SCOPE_ID, delta, now)
        conn = self._connect()
        try:
            episodes = data.get("episodes")
            if isinstance(episodes, list):
                for episode in episodes[:3]:
                    if not isinstance(episode, dict):
                        continue
                    summary = _text(episode.get("summary")).strip()[:300]
                    if not summary:
                        continue
                    with conn:
                        conn.execute("""INSERT INTO episodes(chat_key, at, participants_json, summary,
                                        emotional_valence, unresolved, source_message_ids)
                                        VALUES(?,?,?,?,?,?,?)""",
                                     (chat_key, now, json.dumps(episode.get("participants") or [], ensure_ascii=False),
                                      summary, _clamp(episode.get("emotional_valence"), -100, 100, 0),
                                      1 if episode.get("unresolved") else 0,
                                      json.dumps(episode.get("source_message_ids") or [], ensure_ascii=False)[:1000]))
        finally:
            conn.close()
