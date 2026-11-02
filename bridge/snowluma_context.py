"""Structured conversation envelope.

The model should never receive hand-concatenated attribution strings.  Build a
small, explicit envelope once, then render it deterministically.
"""
from datetime import datetime


def _text(value):
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _speaker_name(event):
    return _text(event.get("sender_name")) or _text(event.get("user_id"))


def build_envelope(service, event, config, text, quote=None, media=None,
                   addressed=False, reply_target=None, recent_speakers=None,
                   group_context="", image_meme="", image_types=""):
    snapshot = service.state_store.snapshot()
    qq = snapshot.get("qq") or {}
    self_id = _text(qq.get("selfId"))
    self_name = _text(qq.get("nickname")) or "小鲸鱼"
    at_qqs = [_text(item) for item in (event.get("at_qqs") or []) if _text(item)]
    mentions = [qq for qq in at_qqs if qq.isdigit() and (not self_id or qq != self_id)]
    mentions = list(dict.fromkeys(mentions))
    quote_data = None
    if isinstance(quote, dict):
        quote_data = {
            "available": True,
            "message_id": _text(quote.get("message_id")),
            "sender_qq": _text(quote.get("user_id")),
            "sender_name": _text(quote.get("nickname")) or _text(quote.get("user_id")),
            "text": _text(quote.get("text"))[:400],
        }
    elif event.get("message_id") and event.get("raw", {}).get("message"):
        # Only mark unavailable when the current event really carries a reply
        # segment; the caller may pass quote=None for ordinary messages.
        for segment in event.get("raw", {}).get("message") or []:
            if isinstance(segment, dict) and _text(segment.get("type")) == "reply":
                quote_data = {"available": False, "message_id": _text((segment.get("data") or {}).get("id")),
                              "sender_qq": "", "sender_name": "", "text": ""}
                break
    media_rows = []
    for item in media or []:
        if isinstance(item, dict):
            media_rows.append({key: item.get(key) for key in ("type", "sub_type", "summary", "transcript", "expanded") if item.get(key) is not None})
    return {
        "v": 2,
        "scene": _text(event.get("message_type")),
        "chat_key": "group:%s" % event.get("group_id") if event.get("message_type") == "group" else "private:%s" % event.get("user_id"),
        "message_id": _text(event.get("message_id")),
        "sender": {"qq": _text(event.get("user_id")), "name": _speaker_name(event)},
        "self": {"qq": self_id, "name": self_name},
        "text": _text(text)[:2000],
        "mentions": mentions,
        "addressed_to_bot": bool(addressed),
        "reply_target": _text(reply_target) or None,
        "quote": quote_data,
        "media": media_rows,
        "recent_speakers": [dict(item) for item in (recent_speakers or [])[-6:] if isinstance(item, dict)],
        "group_context": _text(group_context)[:3000],
        "image_meme": _text(image_meme)[:500],
        "image_types": _text(image_types)[:800],
        "received_at": _text(event.get("time")),
        "rendered_at": datetime.now().isoformat(timespec="seconds"),
    }


def render(envelope):
    if not isinstance(envelope, dict):
        return ""
    lines = []
    sender = envelope.get("sender") or {}
    self_info = envelope.get("self") or {}
    scene = envelope.get("scene") or ""
    if scene == "group":
        lines.append("[对话场景] 群聊")
    else:
        lines.append("[对话场景] 私聊")
    lines.append("[你] %s(%s)" % (self_info.get("name") or "小鲸鱼", self_info.get("qq") or "?"))
    lines.append("[当前消息｜发送者] %s(%s)" % (sender.get("name") or "?", sender.get("qq") or "?"))
    lines.append("[是否直接对你] %s" % ("是" if envelope.get("addressed_to_bot") else "否"))
    mentions = envelope.get("mentions") or []
    if mentions:
        lines.append("[本条@的人] " + "、".join(mentions))
    quote = envelope.get("quote")
    if isinstance(quote, dict):
        if quote.get("available"):
            lines.append("[引用消息｜原发送者] %s(%s)" % (quote.get("sender_name") or "?", quote.get("sender_qq") or "?"))
            lines.append("[引用内容] " + (quote.get("text") or "[无文字]"))
        else:
            lines.append("[引用消息] 原内容不可见，不要猜测引用内容")
    recent = envelope.get("recent_speakers") or []
    if recent:
        rows = []
        for item in recent[-4:]:
            who = _text(item.get("name")) or _text(item.get("user"))
            snippet = _text(item.get("text"))[:60]
            if who:
                rows.append("- %s：%s" % (who, snippet or "[图片/表情]"))
        if rows:
            lines.append("[最近发言]\n" + "\n".join(rows))
    if envelope.get("group_context"):
        lines.append("[群聊补充（可能不是对你说的）]\n" + envelope.get("group_context"))
    if envelope.get("image_types"):
        lines.append("[媒体类型] " + envelope.get("image_types"))
    if envelope.get("image_meme"):
        lines.append("[图片梗] " + envelope.get("image_meme"))
    media = envelope.get("media") or []
    if media:
        parts = []
        for item in media:
            kind = _text(item.get("type"))
            if kind == "voice" and item.get("transcript"):
                parts.append("语音：" + _text(item.get("transcript"))[:600])
            elif kind == "forward":
                parts.append("合并转发：" + (_text(item.get("expanded"))[:800] or "内容未能展开"))
            elif kind in ("image", "mface") and not envelope.get("image_types"):
                parts.append("图片：%s" % _text(item.get("summary")))
        if parts:
            lines.append("[本条媒体]\n" + "\n".join(parts))
    lines.append("[当前消息内容]")
    lines.append(envelope.get("text") or "[没有文字]")
    return "\n".join(lines)


def history_text(entry):
    if isinstance(entry, dict):
        if entry.get("envelope"):
            return render(entry["envelope"])
        return _text(entry.get("content"))
    return _text(entry)
