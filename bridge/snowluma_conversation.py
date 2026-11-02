#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append-only daily conversation transcript used for prompt-cache replay.

The bridge's legacy history is a rolling window of rendered envelopes.  That
window shifts at the start, so the next request cannot reuse the previous
request's prefix.  This store keeps every allowed chat message for the current
"day" in original order and also stores the exact user text/images that were
sent to the model.  The prompt builder can then replay completed turns
verbatim and only append the current turn at the end.
"""
import json
import os
import sqlite3
import threading
import time


def _text(value):
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ConversationStore:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()

    def _path(self):
        state_dir = getattr(self.service, "state_dir", "/opt/snowluma/bridge")
        return os.path.join(state_dir, "conversation.db")

    def _connect(self):
        conn = sqlite3.connect(self._path(), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=8000")
        except Exception:
            pass
        return conn

    def _cfg(self):
        try:
            config = self.service.config_store.get()
        except Exception:
            config = {}
        behavior = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
        context = behavior.get("context") if isinstance(behavior.get("context"), dict) else {}
        return context if isinstance(context, dict) else {}

    def day_key(self, at_ms=None):
        try:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo
            at_ms = _int(at_ms, int(time.time() * 1000))
            config = self.service.config_store.get()
            tz = ZoneInfo(_text(config.get("timeZone")) or "Asia/Shanghai")
            start = max(0, min(23, _int(self._cfg().get("dayStartHour"), 7)))
            dt = datetime.fromtimestamp(at_ms / 1000.0, tz)
            if dt.hour < start:
                dt = dt - timedelta(days=1)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return time.strftime("%Y-%m-%d")

    def ensure_schema(self):
        conn = self._connect()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_key TEXT NOT NULL,
                day_key TEXT NOT NULL,
                role TEXT NOT NULL,
                at INTEGER NOT NULL,
                message_id TEXT NOT NULL DEFAULT '',
                sender_id TEXT NOT NULL DEFAULT '',
                sender_name TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                media_json TEXT NOT NULL DEFAULT '[]',
                prompt_text TEXT NOT NULL DEFAULT '',
                prompt_images_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'auto',
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_turns_chat_day ON turns(chat_key, day_key, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_user_message
                ON turns(chat_key, message_id) WHERE role='user' AND message_id != '';
            """)
            conn.commit()
        finally:
            conn.close()

    def record_user(self, chat_key, at, message_id, sender_id, sender_name, text, media=None, source="auto"):
        chat_key = _text(chat_key)
        if not chat_key:
            return 0
        at = _int(at, int(time.time() * 1000))
        message_id = _text(message_id).strip()
        conn = self._connect()
        try:
            if message_id:
                row = conn.execute(
                    "SELECT id FROM turns WHERE chat_key=? AND message_id=? AND role='user'",
                    (chat_key, message_id)).fetchone()
                if row:
                    return _int(row["id"])
            day_key = self.day_key(at)
            media_list = media if isinstance(media, list) else []
            with conn:
                cur = conn.execute(
                    "INSERT INTO turns(chat_key, day_key, role, at, message_id, sender_id, sender_name,"
                    " text, media_json, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (chat_key, day_key, "user", at, message_id, _text(sender_id), _text(sender_name),
                     _text(text)[:4000], json.dumps(media_list[:8], ensure_ascii=False), _text(source) or "auto",
                     int(time.time() * 1000)))
                return _int(cur.lastrowid)
        finally:
            conn.close()

    def set_prompt(self, turn_id, prompt_text, image_paths):
        turn_id = _int(turn_id)
        if not turn_id:
            return False
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE turns SET prompt_text=?, prompt_images_json=? WHERE id=?",
                    (_text(prompt_text)[:200000],
                     json.dumps([_text(item) for item in (image_paths or []) if _text(item)][:8], ensure_ascii=False),
                     turn_id))
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def record_assistant(self, chat_key, at, text, source="auto", message_id=""):
        chat_key = _text(chat_key)
        text = _text(text).strip()
        if not chat_key or not text:
            return 0
        at = _int(at, int(time.time() * 1000))
        conn = self._connect()
        try:
            day_key = self.day_key(at)
            with conn:
                cur = conn.execute(
                    "INSERT INTO turns(chat_key, day_key, role, at, message_id, text, source, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (chat_key, day_key, "assistant", at, _text(message_id), text[:4000],
                     _text(source) or "auto", int(time.time() * 1000)))
                return _int(cur.lastrowid)
        finally:
            conn.close()

    def list_turns(self, chat_key, day_key=None, exclude_id=0, min_id=0):
        chat_key = _text(chat_key)
        if not chat_key:
            return []
        conn = self._connect()
        try:
            sql = ("SELECT id, chat_key, day_key, role, at, message_id, sender_id, sender_name, text,"
                   " media_json, prompt_text, prompt_images_json, source FROM turns"
                   " WHERE chat_key=?")
            params = [chat_key]
            if day_key:
                sql += " AND day_key=?"
                params.append(_text(day_key))
            if min_id:
                sql += " AND id>=?"
                params.append(_int(min_id))
            if exclude_id:
                sql += " AND id<>?"
                params.append(_int(exclude_id))
            sql += " ORDER BY id ASC"
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def max_turn_id(self, chat_key):
        conn = self._connect()
        try:
            row = conn.execute("SELECT MAX(id) AS max_id FROM turns WHERE chat_key=?", (_text(chat_key),)).fetchone()
            return _int(row["max_id"] if row else 0)
        finally:
            conn.close()

    def clear_chat(self, chat_key):
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM turns WHERE chat_key=?", (_text(chat_key),))
        finally:
            conn.close()

    def purge_before(self, day_key):
        day_key = _text(day_key)
        if not day_key:
            return 0
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM turns WHERE day_key < ?", (day_key,))
                return cur.rowcount
        finally:
            conn.close()

    def estimate_chars(self, turns):
        total = 0
        for row in turns or []:
            total += len(_text(row.get("prompt_text")) or _text(row.get("text")))
        return total

