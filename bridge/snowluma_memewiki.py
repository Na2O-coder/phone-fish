"""Local meme wiki (MIT, data from in1t1a1/astrbot_plugin_memewiki).
Read-only lookup over the bundled memes.db, with compact usage-first context.
"""

import os
import re
import sqlite3
import threading


DEFAULT_DB = "/opt/snowluma/bridge/data/memes.db"


def _clean(text):
    return re.sub(r"\s+", "", (text or "")).strip()


class MemeWiki:
    def __init__(self, service):
        self.service = service
        self._db_path = ""
        self._mtime = 0
        self._titles = []
        self._titles_at = 0
        self._titles_lock = threading.Lock()

    def _cfg(self):
        try:
            config = self.service.config_store.get()
        except Exception:
            config = {}
        return (config.get("skills") or {}).get("memewiki") or {}

    def db_path(self):
        cfg = self._cfg()
        path = str(cfg.get("dbPath") or DEFAULT_DB)
        if os.path.isfile(path):
            return path
        state_dir = getattr(self.service, "state_dir", "") or ""
        fallback = os.path.join(state_dir, "data", "memes.db")
        if os.path.isfile(fallback):
            return fallback
        return path

    def lookup(self, term):
        term = _clean(term)
        if not term:
            return None
        path = self.db_path()
        if not os.path.isfile(path):
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT title, slug, tags, usage, origin, other, summary FROM memes "
                "WHERE title = ? OR slug = ? LIMIT 1", (term, term)).fetchone()
            if row is None and len(term) >= 2:
                row = conn.execute(
                    "SELECT title, slug, tags, usage, origin, other, summary FROM memes "
                    "WHERE title LIKE ? LIMIT 1", ("%" + term + "%",)).fetchone()
            conn.close()
            return dict(row) if row is not None else None
        except Exception:
            return None

    def _load_titles(self):
        path = self.db_path()
        if not os.path.isfile(path):
            return []
        with self._titles_lock:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                return []
            if self._titles and self._titles_at == mtime:
                return self._titles
            titles = []
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3)
                for row in conn.execute("SELECT title, slug FROM memes"):
                    for value in row:
                        value = str(value or "").strip()
                        if len(value) >= 2 and value not in titles:
                            titles.append(value)
                conn.close()
            except Exception:
                titles = []
            self._titles = titles
            self._titles_at = mtime
            return titles

    def scan_terms(self, text, limit=2):
        body = _clean(text)
        if not body or len(body) > 80:
            return []
        # Casual Chinese often adds 里/啊/了 around the same meme phrase.
        norm_body = re.sub(r"[里啊呀呢吧哦嘛啦]", "", body)
        skip = {"笑死", "确实", "真的", "不是", "可以", "谢谢", "你好", "在吗", "晚安", "早安"}
        found = []
        for title in self._load_titles():
            if title in skip or len(title) < 4:
                continue
            norm_title = re.sub(r"[里啊呀呢吧哦嘛啦]", "", title)
            if (title in body or (len(norm_title) >= 4 and norm_title in norm_body)) and title not in found:
                found.append(title)
                if len(found) >= limit:
                    break
        return found

    @staticmethod
    def format_hint(row, max_chars=420):
        if not row:
            return ""
        title = str(row.get("title") or row.get("slug") or "").strip()
        usage = str(row.get("usage") or "").strip()
        if not usage:
            return ""
        return ("【梗】" + title + "\n怎么用：" + usage[:max_chars]).rstrip()

    @staticmethod
    def format_entry(row, max_chars=650):
        if not row:
            return ""
        title = str(row.get("title") or row.get("slug") or "").strip()
        usage = str(row.get("usage") or "").strip()
        origin = str(row.get("origin") or "").strip()
        tags = str(row.get("tags") or "").strip()
        summary = str(row.get("summary") or "").strip()
        if not (usage or origin or summary):
            return ""
        lines = ["【梗】" + title]
        if usage:
            lines.append("怎么用：" + usage[:max(120, max_chars // 2)])
        if origin:
            lines.append("出处：" + origin[:max(80, max_chars // 3)])
        elif summary:
            lines.append("摘要：" + summary[:max(80, max_chars // 3)])
        if tags:
            lines.append("标签：" + tags[:120])
        text = "\n".join(lines)
        return text[:max_chars].rstrip()

    def term_from_text(self, text):
        body = _clean(text)
        body = body.strip("，。！？!?、；;：")
        if not body or len(body) > 40 or body.startswith("/"):
            return ""
        # "X是什么梗 / X啥意思 / X的出处 / X怎么来的"
        match = re.search(r"^(.{1,20}?)(?:是什么梗|什么梗|啥意思|什么意思|是什么意思|的出处|出处|怎么来的|什么来头|是谁)$", body)
        if match:
            return match.group(1).strip("，。！？!?、 ")
        # 裸短词：只对很短、没有标点/动作句的文本尝试
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_+\-]{1,10}", body):
            return body
        return ""

    def context(self, text, max_chars=650):
        term = self.term_from_text(text)
        row = self.lookup(term) if term else None
        if row:
            try:
                self.service.log("梗百科命中: %s" % term)
            except Exception:
                pass
            return self.format_hint(row, max_chars=max_chars)
        # Not an explicit/short query: scan for titles embedded in a normal sentence.
        # This is a hint only; the model decides whether the message is actually using the meme.
        for candidate in self.scan_terms(text):
            row = self.lookup(candidate)
            if row:
                try:
                    self.service.log("梗百科提示: %s" % candidate)
                except Exception:
                    pass
                return "（以下只是可能相关的梗，不一定在玩梗，自行判断）\n" + self.format_hint(row, max_chars=max_chars)
        return ""
