"""Single source of truth for the companion system prompt.

The prompt is stored as versioned markdown files next to the bridge.  Keeping it
out of config.json makes persona edits reviewable and prevents stale tool
instructions from silently surviving config migrations.
"""
import hashlib
import os
import threading

_CACHE = {"key": None, "text": "", "version": ""}
_LOCK = threading.Lock()
FILES = ("identity.md", "behavior.md", "tokens.md", "safety.md")


def _prompt_dir(service, config):
    behavior = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
    path = behavior.get("promptDir") if isinstance(behavior.get("promptDir"), str) else ""
    if path and os.path.isdir(path):
        return path
    state_dir = getattr(service, "state_dir", "/opt/snowluma/bridge")
    return os.path.join(state_dir, "prompts")


def _mtimes(directory):
    values = []
    for name in FILES:
        path = os.path.join(directory, name)
        try:
            values.append((name, os.path.getmtime(path), os.path.getsize(path)))
        except OSError:
            values.append((name, 0, 0))
    return tuple(values)


def load_bundle(service, config):
    directory = _prompt_dir(service, config)
    key = (directory, _mtimes(directory))
    with _LOCK:
        if _CACHE.get("key") == key and _CACHE.get("text"):
            return dict(_CACHE)
        parts = []
        digest = hashlib.sha256()
        for name in FILES:
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    body = handle.read().strip()
            except OSError:
                body = ""
            if body:
                digest.update(("## " + name + "\n").encode("utf-8"))
                digest.update(body.encode("utf-8"))
                parts.append(body)
        text = "\n\n".join(parts)
        result = {"key": key, "text": text, "version": digest.hexdigest()[:12]}
        _CACHE.update(result)
        return dict(result)
