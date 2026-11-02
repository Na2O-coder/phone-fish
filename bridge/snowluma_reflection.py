"""Post-turn reflection: model-maintained relationship/memory updates.

No keyword lists.  The model sees a compact transcript and returns structured
JSON; invalid output is dropped without changing state.
"""
import json
import queue
import re
import threading
import time


def _text(value):
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _clamp(value, low, high, default=0):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class ReflectionEngine:
    def __init__(self, service):
        self.service = service
        self.queue = queue.Queue(maxsize=100)
        self.thread = None
        self.stop_event = threading.Event()
        self.last_by_chat = {}
        self.count_by_chat = {}

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="snowluma-reflection", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self.thread is not None:
            self.thread.join(timeout=1.0)

    def _cfg(self):
        config = self.service.config_store.get()
        behavior = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
        return behavior.get("reflection") if isinstance(behavior.get("reflection"), dict) else {}

    def consider(self, envelope, reply, chat_key, scene, target, user_id):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return
        every = max(1, int(cfg.get("everyN", 3) or 3))
        cooldown = max(60, int(cfg.get("cooldownSeconds", 600) or 600)) * 1000
        now = int(time.time() * 1000)
        count = self.count_by_chat.get(chat_key, 0) + 1
        self.count_by_chat[chat_key] = count
        if count % every != 0:
            return
        if now - self.last_by_chat.get(chat_key, 0) < cooldown:
            return
        self.last_by_chat[chat_key] = now
        try:
            self.queue.put_nowait({"envelope": envelope, "reply": reply, "chat_key": chat_key,
                                   "scene": scene, "target": target, "user_id": user_id})
        except queue.Full:
            pass

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                item = self.queue.get(timeout=5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._reflect(item)
            except Exception as error:
                try:
                    self.service.log("反思失败: %s: %s" % (error.__class__.__name__, error))
                except Exception:
                    pass

    def _history_text(self, chat_key):
        try:
            rows = self.service.auto_engine._history_messages(chat_key, 8)
        except Exception:
            rows = []
        lines = []
        for row in rows:
            content = _text(row.get("content")).strip()
            if not content:
                continue
            if _text(row.get("role")) == "assistant":
                content = "【你刚才的回复】" + content
            lines.append(content[:500])
        return "\n".join(lines[-6:])

    def _reflect(self, item):
        config = self.service.config_store.get()
        envelope = item.get("envelope") or {}
        chat_key = _text(item.get("chat_key"))
        history = self._history_text(chat_key)
        current = json.dumps(envelope, ensure_ascii=False)[:4000]
        bot_names = []
        try:
            group_reply = ((config.get("skills") or {}).get("groupReply") or {})
            bot_names = [_text(item) for item in (group_reply.get("botNames") or []) if _text(item)]
        except Exception:
            bot_names = []
        bot_label = "、".join(bot_names[:8]) or "小鲸鱼"
        try:
            qq_state = self.service.state_store.snapshot().get("qq") or {}
        except Exception:
            qq_state = {}
        bot_qq = _text(config.get("botQQ")) or _text(qq_state.get("selfId")) or "机器人"
        system = (
            ("你是小鲸鱼（QQ %s；群昵称/别名：%s），对话里的 [你] 指你自己，不是主人或群友。" % (bot_qq, bot_label)) +
            "你是虚拟陪伴的关系与记忆整理器。只输出 JSON，不要解释。"
            "根据最近对话、当前消息和她的回复，输出结构："
            '{"episodes":[{"summary":"一句事实摘要","participants":["QQ"],"unresolved":false,"emotional_valence":0}],'
            '"relationship_deltas":[{"qq":"QQ","familiarity_delta":0,"trust_delta":0,"tension_delta":0,"reason":"简短原因"}],'
            '"mood_delta":0,"memory_candidates":[{"content":"事实","kind":"fact|event|preference|promise|relationship",'
            '"scope":"user|group","subject_qq":"QQ","importance":50,"confidence":80}],'
            '"corrections":[{"id":0,"content":"修正后的事实","reason":"原因"}],'
            '"rule_candidates":[{"title":"短标题","proposal":"以后应该怎么做的长期规则","reason":"为什么"}]}'
            "规则：只根据明确出现的事实；不猜测私密信息；无变化就给空数组/0；最多 3 个 episode、5 个 delta、5 条记忆、3 条纠正、1 条规则候选。"
            "只有用户明确纠正你的行为/提出长期相处方式时才提 rule_candidate；普通玩笑、一次性请求不要提。"
            "如果说话者是主人，并指出你说话方式不好、不讨喜、情商低、以后别这样，即使没说“记住”，也算明确纠正，应提 1 条 rule_candidate。"
            "如果已有待确认/已通过规则已经覆盖同一件事，就不要再提 rule_candidate；只有这次出现明确的新修正、需要补充时才提，并在 reason 里写清和已有规则的区别。"
            "戳一戳、互怼、起哄是社交动作：先判断是玩笑还是明确冲突，不要机械扣分；只有明确造成紧张/伤害才调低关系。"
            "写 rule_candidate 前先确认主语和宾语：是“别人怎么叫你/怎么对你”，还是“你怎么叫别人/怎么对别人”。"
            "不要把别人给你的称呼或待遇，写成你去称呼/对待别人；拿不准就写成“当某人这样称呼我/对我时，我……”。"
            "看 [当前消息｜发送者]、[引用消息｜原发送者]、[引用内容] 判断谁对谁说了什么。"
            "例如 A 对 B 说“叫你 X 就行”，是在指定 A 怎么称呼 B，不要写成“称呼 A 为 X”。"
            "输出前自查 rule_candidate：谁在说、谁被称呼、你要改变的是哪一方；方向拿不准就不提，不要猜。"
            "rule_candidate 的 proposal 尽量用第一人称写清：当谁对我做什么时，我该怎么做；不要写成“以后称呼谁为……”。"
            "主人 QQ：%s。" % "、".join([_text(x) for x in (((config.get("permissions") or {}).get("ownerUsers") or []))])
        )
        social_lines = []
        try:
            events = (self.service.state_store.snapshot().get("socialEvents") or [])
            now = int(time.time() * 1000)
            for event in reversed(events):
                if not isinstance(event, dict) or _text(event.get("chat_key")) != chat_key:
                    continue
                at = int(event.get("at") or 0)
                if at and now - at > 15 * 60 * 1000:
                    continue
                social_lines.append("- %s（%s）%s" % (
                    _text(event.get("nickname")) or _text(event.get("user_id")),
                    "戳了你一下" if _text(event.get("action")) == "poke" else _text(event.get("action")),
                    _text(event.get("detail"))[:60]))
                if len(social_lines) >= 6:
                    break
        except Exception:
            social_lines = []
        rule_lines = []
        try:
            snapshot = self.service.state_store.snapshot()
            for entry in (snapshot.get("ruleCandidates") or [])[-5:]:
                if isinstance(entry, dict):
                    proposal = _text(entry.get("proposal")).strip()
                    if proposal:
                        rule_lines.append("- 待确认：" + proposal[:120])
            for entry in (snapshot.get("approvedRules") or [])[-8:]:
                if isinstance(entry, dict):
                    proposal = _text(entry.get("proposal")).strip()
                    if proposal:
                        rule_lines.append("- 已通过：" + proposal[:120])
        except Exception:
            rule_lines = []
        user = "最近对话：\n" + history
        if social_lines:
            user += "\n\n最近社交动作（只是背景，判断玩笑/冲突）：\n" + "\n".join(reversed(social_lines))
        if rule_lines:
            user += "\n\n已有规则（同一件事不要重复提）：\n" + "\n".join(rule_lines)
        user += "\n\n当前消息 envelope：\n" + current + "\n\n她的回复：\n" + _text(item.get("reply"))[:1000]
        try:
            from snowluma_service import call_deepseek
            result = call_deepseek(config, [{"role": "system", "content": system},
                                            {"role": "user", "content": user}], effort_override="low")
        except Exception as error:
            self.service.log("反思模型调用失败: %s" % error)
            return
        if not result.get("ok"):
            self.service.log("反思模型无结果: %s" % result.get("error"))
            return
        reply = _text(result.get("reply")).strip()
        fence = chr(96) + chr(96) + chr(96)
        reply = reply.replace(fence + "json", "").replace(fence, "").strip()
        match = re.search(r"\{[\s\S]*\}", reply)
        if not match:
            return
        try:
            data = json.loads(match.group(0))
        except Exception:
            self.service.log("反思 JSON 解析失败")
            return
        if not isinstance(data, dict):
            return
        relationships = getattr(self.service, "relationships", None)
        if relationships is not None:
            relationships.apply(chat_key, _text(item.get("scene")), _text(item.get("user_id")), data)
        mood_delta = _clamp(data.get("mood_delta"), -5, 5, 0)
        if mood_delta and hasattr(self.service, "mood"):
            self.service.mood.change(mood_delta, "反思")
        self._apply_memories(data, item)
        try:
            candidates = data.get("rule_candidates")
            if isinstance(candidates, list):
                import snowluma_skills as skills_ext
                config = self.service.config_store.get()
                for candidate in candidates[:1]:
                    if not isinstance(candidate, dict):
                        continue
                    proposal = _text(candidate.get("proposal")).strip()
                    if len(proposal) < 6 or len(proposal) > 500:
                        continue
                    skills_ext.propose_rule(
                        self.service, config,
                        _text(candidate.get("title")) or proposal[:40],
                        proposal, _text(candidate.get("reason")),
                        scope=chat_key, source="reflection")
        except Exception as error:
            self.service.log("规则候选处理失败: %s" % error)
        self.service.log("反思完成: chat=%s episode=%d delta=%d" % (
            chat_key, len(data.get("episodes") or []), len(data.get("relationship_deltas") or [])))

    def _apply_memories(self, data, item):
        cfg = self.service.config_store.get()
        memory_cfg = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
        if not memory_cfg.get("enabled", True):
            return
        chat_key = _text(item.get("chat_key"))
        scene = _text(item.get("scene"))
        user_id = _text(item.get("user_id"))
        group_id = chat_key.split(":", 1)[1] if chat_key.startswith("group:") else ""
        candidates = data.get("memory_candidates")
        if isinstance(candidates, list):
            for candidate in candidates[:5]:
                if not isinstance(candidate, dict):
                    continue
                content = _text(candidate.get("content")).strip()
                if len(content) < 4 or len(content) > 300:
                    continue
                kind = _text(candidate.get("kind"))
                if kind not in ("fact", "event", "preference", "promise", "relationship"):
                    kind = "fact"
                scope = _text(candidate.get("scope"))
                subject = _text(candidate.get("subject_qq")) or user_id
                importance = _clamp(candidate.get("importance"), 0, 100, 50)
                confidence = _clamp(candidate.get("confidence"), 0, 100, 75)
                if confidence < 45:
                    continue
                if scope == "group" and group_id:
                    self.service.memories.add("group", group_id, subject, content, kind, importance, confidence, False, "reflection")
                else:
                    self.service.memories.add("user", subject, subject, content, kind, importance, confidence, False, "reflection")
        corrections = data.get("corrections")
        if isinstance(corrections, list):
            for correction in corrections[:3]:
                if not isinstance(correction, dict):
                    continue
                try:
                    memory_id = int(correction.get("id"))
                except (TypeError, ValueError):
                    continue
                content = _text(correction.get("content")).strip()
                if not content:
                    continue
                self.service.memories.forget(memory_id)
                subject = _text(correction.get("subject_qq")) or user_id
                scope = "group" if (scene == "group" and group_id) else "user"
                scope_id = group_id if scope == "group" else subject
                self.service.memories.add(scope, scope_id, subject, content, "fact", 70, 90, False, "reflection")
                self.service.log("记忆纠正: id=%s" % memory_id)
