#!/usr/bin/env python3
"""发布前隐私/密钥扫描。退出码 1 表示发现疑似问题。"""
import argparse
import json
import re
import sys
from pathlib import Path

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".service", ".sh", ".example", ".js", ".mjs", ".ts", ".css", ".html", ".svg"}
FORBIDDEN_NAMES = {
    "state.json", "memory.db", "conversation.db", "bridge.log",
    "cookies.txt", "bilibili-cookies-netscape.txt", "douyin-cookies-netscape.txt",
}
FORBIDDEN_SUFFIXES = {".pyc", ".log", ".db-wal", ".db-shm"}

SECRET_PATTERNS = [
    ("deepseek-or-generic-key", re.compile(r"sk-[A-Za-z0-9_.-]{16,}")),
    ("bridge-token", re.compile(r"vm_bridge_token_[A-Za-z0-9_]+")),
    ("old-public-ip", re.compile(r"10\.188\.57\.64")),
    ("snowluma-tarball", re.compile(r"snowluma-arm64\.tar")),
]

JSON_SECRET_KEYS = {"apiKey", "bridgeToken", "onebotToken", "api_key", "token", "password", "secret"}
PLACEHOLDER_HINTS = ("", "YOUR_", "<", "sk-...", "sk-test", "change-me", "TODO")


def text_files(root):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES:
            yield path, None
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {".gitignore", "LICENSE", "NOTICE", "README"}:
            try:
                yield path, path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                yield path, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--private-patterns", default="", help="可选：额外正则文件（每行一条），不会进入仓库")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    issues = []

    extra = []
    if args.private_patterns and Path(args.private_patterns).is_file():
        for line in Path(args.private_patterns).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    extra.append((line, re.compile(line)))
                except re.error:
                    issues.append("非法私密模式: " + line)

    for path, content in text_files(root):
        rel = path.relative_to(root).as_posix()
        if content is None:
            issues.append("禁止的二进制/运行时文件: " + rel)
            continue
        for name, pattern in SECRET_PATTERNS + extra:
            if pattern.search(content):
                issues.append("%s: %s" % (rel, name))
        if path.suffix == ".json":
            try:
                data = json.loads(content)
            except Exception:
                continue
            def walk(value, prefix=""):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key in JSON_SECRET_KEYS and isinstance(item, str) and item.strip():
                            if not any(item.strip().startswith(hint) for hint in PLACEHOLDER_HINTS if hint):
                                issues.append("%s: JSON 密钥字段非空 %s%s" % (rel, prefix, key))
                        walk(item, prefix + key + ".")
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        walk(item, prefix + str(index) + ".")
            walk(data)
        if rel.endswith("config.example.json"):
            try:
                cfg = json.loads(content)
                if cfg.get("permissions", {}).get("ownerUsers"):
                    issues.append(rel + ": ownerUsers 不是空")
                if cfg.get("skills", {}).get("groupAdmin", {}).get("adminUsers"):
                    issues.append(rel + ": adminUsers 不是空")
                if cfg.get("botQQ"):
                    issues.append(rel + ": botQQ 不是空")
            except Exception:
                pass

    # 目录里直接出现 .env / config.json / 数据库
    for path in root.rglob("*"):
        if path.is_file() and path.name in {".env", "config.json"}:
            issues.append("禁止提交本机配置: " + path.relative_to(root).as_posix())

    if issues:
        print("[redact_check] FAILED")
        for issue in sorted(set(issues)):
            print(" - " + issue)
        return 1
    print("[redact_check] OK: no known secret/runtime patterns in " + str(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
