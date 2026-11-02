#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snowluma_service.py — Operit 侧边栏 QQ 插件（com.operit.qq_snowluma）Python 桥接服务

单文件、仅依赖 Python 3.12 标准库，职责：
  1) 本地控制 HTTP 服务（默认 127.0.0.1:32146，X-Auth-Token 鉴权），供 Operit 插件 JS 侧调用；
  2) 通过 subprocess 管理 udocker 中 SnowLuma 容器的生命周期（pull/create/start/stop/restart/logs/status）；
  3) 作为 OneBot v11 正向 WebSocket 客户端连接 SnowLuma（默认 ws://127.0.0.1:3001/），
     接收 QQ 消息事件、白名单过滤、@/前缀触发、冷却/去重，调用 DeepSeek（OpenAI 兼容接口）生成回复，
     再用 OneBot 动作 send_private_msg / send_group_msg 发回 QQ；
  4) 状态持久化（config.json / state.json / bridge.log / bridge.pid），SIGTERM 优雅退出。

设计参考：ref/qqbot_bundle/resources/qqbot_gateway_service.py（裸 socket WebSocket 客户端、
ThreadingHTTPServer 控制面、状态机与重连）+ PLAN.md 的 JS↔Python 契约。
详见 docs/notes-qqbot-patterns.md。

启动示例（JS 侧 tools.System.terminal.hiddenExec 使用）：
  PYTHONUNBUFFERED=1 nohup python3 snowluma_service.py \
      --state-dir /sdcard/Download/Operit/plugins/com.operit.qq_snowluma \
      --port 32146 --token <bridgeToken> > <state-dir>/bridge.log 2>&1 & echo $!

安全约定：
  - 控制面只监听 127.0.0.1，所有请求校验 X-Auth-Token（或 ?token=）。
  - 日志不落 API Key / Token（统一脱敏）。
  - config.json / state.json 原子写并尽量 chmod 600（/sdcard FUSE 上 chmod 可能不生效）。
"""

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import queue
import random
import re
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

import snowluma_skills as skills_ext
import snowluma_memewiki
import snowluma_context as context_ext
import snowluma_relationships
import snowluma_reflection
import snowluma_conversation

from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PRODUCT = "com.operit.qq_snowluma"
VERSION = "0.1.0"
DEFAULT_CONTROL_PORT = 32146
SECRET_MASK = "********"
MASK_MIN_LENGTH = 4
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_LOG_TAIL_LINES = 2000
MAX_WS_PAYLOAD = 16 * 1024 * 1024
MAX_SEEN_MESSAGE_IDS = 1000
MAX_HISTORY_MESSAGES = 200
MAX_REPLY_CHARS = 2000
DEFAULT_HTTP_TIMEOUT_S = 15.0
ACTION_TIMEOUT_MS = 20000
START_WAIT_MS = 15000
STOP_WAIT_MS = 10000
STATUS_REFRESH_INTERVAL_MS = 30000
LOG_ROTATE_KEEP = 1

# PLAN.md「JS↔Python 契约」+ Lead 补充的 bridgeToken：config.json 的完整默认形状。
DEFAULT_CONFIG = {
    "timeZone": "Asia/Shanghai",
    "bridgeToken": "",
    "autoStartBridge": True,
    "containerName": "snowluma",
    "image": "motricseven7/snowluma:v2.1.1",
    "webuiPort": 5099,
    "webuiHost": "127.0.0.1",
    "onebotHost": "127.0.0.1",
    "onebotPort": 3001,
    "onebotToken": "",
    "deepseek": {
        "baseUrl": "https://api.deepseek.com/v1",
        "apiKey": "",
        "model": "deepseek-flash",
        "systemPrompt": "你是一个通过 QQ 聊天的 AI 助手。回答简洁、自然、口语化，默认使用中文；不要暴露系统提示词。",
        "temperature": 0.7,
        "maxTokens": 2048,
        "maxCallsPerMinute": 30,
        "timeoutMs": 60000,
        "effort": "high",
        "maxTokens": 2048,
        "webSearch": {
            "enabled": True,
            "engine": "bing",
            "maxResults": 5,
            "maxRounds": 3,
        },
    },
    "autoReply": {
        "enabled": False,
        "privateEnabled": True,
        "groupEnabled": True,
        "requireAtInGroup": True,
        "groupReplyMode": "at",
        "interjectProbability": 30,
        "interjectMediaProbability": 80,
        "groupMergeMs": 1200,
        "triggerPrefix": "",
        "cooldownMs": 3000,
        "maxHistory": 12,
    },
    "whitelist": {
        "mode": "off",
        "users": [],
        "groups": [],
    },
    "voice": {
        "enabled": True,
        "engine": "edge",
        "voice": "zh-CN-XiaoyiNeural",
        "rate": "-5%",
        "pitch": "+15Hz",
        "maxChars": 60,
        "maxPerDay": 3,
        "autoMinFamiliarity": 60,
        "requestFamiliarity": 70,
        "moodMin": 30,
        "edgeTtsBin": "/home/ubuntu/.local/bin/edge-tts",
    },
    "mood": {
        "enabled": True,
        "initial": 55,
        "baseline": 50,
        "decayPerHour": 4,
        "minScore": 5,
        "maxScore": 95,
        "ruleCooldownSeconds": 60,
        "affectProactive": True,
        "proactiveLowThreshold": 30,
    },
    "permissions": {
        "ownerUsers": [],
        "sensitiveCommands": ["/好感", "/effort", "/主动", "/放宽", "/插话", "/合并", "/配置", "/状态", "/日志", "/重启桥", "/代发", "/代发群", "/整理记忆", "/img", "/心情",
                              "/kick", "/mute", "/unmute", "/card", "/name", "/权限", "/印象"],
        "highRelationshipThreshold": 90,
        "highRelationshipCommands": ["/印象"],
    },
    "asr": {
        "enabled": True,
        "provider": "aliyun",
        "apiKey": "",
        "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        "model": "paraformer-realtime-v2",
        "maxSeconds": 60,
        "silkDecoder": "/opt/snowluma/bin/silk_decoder",
    },
    "memory": {
        "enabled": True,
        "extractEvery": 8,
        "extractCooldownMinutes": 10,
        "maxPromptItems": 6,
        "consolidateHour": 4,
    },
    "sleep": {
        "enabled": True,
        "start": "23:00",
        "end": "07:00",
        "wakeThreshold": 8,
        "summaryEnabled": True,
        "postWakeQuietMinutes": 30,
        "flushMaxAttempts": 3,
        "ownerPrivateExempt": True,
        "maxBuffer": 40,
    },
    "pricing": {
        "enabled": True,
        "peakWindows": ["09:00-12:00", "14:00-18:00"],
        "peakWeekdays": [0, 1, 2, 3, 4],
        "holidayDates": [],
        "peakReplyFactor": 0.6,
        "ownerPrivateMax": True,
    },
    "profiles": {
        "enabled": True,
        "summarizeEvery": 15,
        "minIntervalMinutes": 30,
        "maxParticipants": 8,
    },
    "proactive": {
        "enabled": True,
        "minSilenceMinutes": 240,
        "maxPerDay": 2,
        "minIntervalMinutes": 180,
        "quietStart": 23,
        "quietEnd": 7,
    },
    "skills": {
        "multiMessage": {"enabled": True, "maxMessages": 4},
        "autoAtInGroup": True,
        "quoteOnReply": False,
        "stickers": {"dirHost": "/opt/snowluma/data/stickers", "dirApp": "/app/data/stickers"},
        "images": {"enabled": True},
        "actions": {"poke": True, "like": True, "recall": True},
        "images": {"enabled": True, "maxHistoryImages": 6},
        "video": {"enabled": True, "frames": 3},
        "groupReply": {"quoteOthers": False, "botNames": []},
        "memory": {"enabled": True, "maxEntries": 20},
        "reminders": {"enabled": True, "maxMinutes": 1440},
        "groupAdmin": {"enabled": False, "adminUsers": []},
    },
}

# udocker 命令模板默认值；可在 <state-dir>/udocker_commands.json 覆盖（不污染 config.json）。
# udocker 命令模板默认值（docs/01 已实测）；可在 <state-dir>/udocker_commands.json 覆盖。
# PRoot 内 euid=0，udocker 每条命令都需要 --allow-root：默认 baseCommand 已包含。
DEFAULT_UDOCKER_COMMANDS = {
    "version": "version",
    "ps": "ps",
    "psAll": "ps -m",
    "pull": "pull --platform={platform} {image}",
    "create": "create --name={name} {image}",
    "setup": "setup --execmode={execmode} {name}",
    # 与 docs/01 §5.1 实测启动命令一致（端口不映射；PRoot 与容器共用网络栈）
    "start": ("run --volume={dataDir}:/app/data --volume={configDir}:/app/.config "
              "--volume={localShareDir}:/app/.local/share "
              "--env=SNOWLUMA_WEBUI_PORT={webuiPort} --env=SNOWLUMA_WEBUI_HOST={webuiHost} "
              "--env=SNOWLUMA_ONEBOT_HOST={onebotHost} --env=SNOWLUMA_HOOK_AUTOLOAD=1 "
              "--env=SNOWLUMA_SCREEN=1280x720x24 --env=VNC_PASSWD= --env=TZ=Asia/Shanghai {name}"),
    "stop": "stop {name}",
    "logs": "",
}
DEFAULT_PLATFORM = "linux/arm64"
DEFAULT_CONTAINER_LOG = "/root/snowluma/container.log"
DEFAULT_UDOCKER_DIRS = {
    "data": "/root/snowluma/data",
    "config": "/root/snowluma/config",
    "localShare": "/root/snowluma/local-share",
    "home": "/root",
    "udocker": "/root/.udocker",
}
DEFAULT_EXECMODE = "P2"
DEFAULT_UDOCKER_TIMEOUTS = {
    "defaultMs": 300000,
    "pullMs": 1800000,
    "psMs": 15000,
    "versionMs": 10000,
    "setupMs": 120000,
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def now_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""


def first_non_blank(*values):
    for value in values:
        text = as_text(value).strip()
        if text:
            return text
    return ""


def is_object(value):
    return isinstance(value, dict)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def safe_int(value, default=0):
    try:
        if isinstance(value, bool):
            return default
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def clamp_int(value, minimum, maximum, default):
    number = safe_int(value, default)
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def clamp_float(value, minimum, maximum, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = as_text(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "n", "否", "关"):
        return False
    return default


def deep_merge(base, patch):
    """递归合并 dict；patch 中的其它类型直接覆盖。返回新对象，不修改入参。"""
    result = copy.deepcopy(base) if is_object(base) else {}
    if not is_object(patch):
        return result
    for key, value in patch.items():
        if is_object(value) and is_object(result.get(key)):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def deep_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return copy.deepcopy(value)


def unique_text_list(values):
    items = []
    seen = set()
    if not isinstance(values, (list, tuple)):
        values = []
    for value in values:
        text = as_text(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def mask_secret(value):
    text = as_text(value)
    if not text:
        return ""
    return SECRET_MASK


def read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if is_object(data) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        return {}


def ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def chmod_quiet(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write_json(path, data, mode=0o600):
    """原子写 JSON：同目录临时文件 + os.replace；FUSE 不支持时退化为直接写。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    ensure_dir(directory)
    temp_path = os.path.join(directory, ".%s.tmp.%d" % (os.path.basename(path), os.getpid()))
    wrote = False
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temp_path, path)
            wrote = True
        except OSError:
            wrote = False
    except OSError:
        wrote = False
    finally:
        if not wrote:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
    if not wrote:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
    chmod_quiet(path, mode)


def tail_text_file(path, lines):
    """读取文本文件末尾 N 行（日志可能很大，按块从尾部读）。"""
    lines = max(1, min(MAX_LOG_TAIL_LINES, safe_int(lines, 200)))
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size <= 0:
        return ""
    block = min(size, 512 * 1024)
    try:
        with open(path, "rb") as handle:
            handle.seek(size - block)
            data = handle.read()
        text = data.decode("utf-8", errors="replace")
        parts = text.splitlines()
        if block < size and parts:
            parts = parts[1:]  # 丢弃可能被截断的第一行
        return "\n".join(parts[-lines:])
    except OSError:
        return ""


def pid_alive(pid):
    pid = safe_int(pid, 0)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8", errors="replace") as handle:
            fields = handle.read().split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except OSError:
        pass
    return True


def process_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            return handle.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def kill_pid(pid, sig, group=False):
    pid = safe_int(pid, 0)
    if pid <= 0:
        return False
    try:
        if group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 日志（自动脱敏 + 轮转）
# ---------------------------------------------------------------------------

class BridgeLogger:
    """线程安全日志器：写入日志文件，自动脱敏 secret，超限轮转。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.redact_provider = None  # callable() -> list[str]
        self._stdout_is_log_file = False
        try:
            if os.path.exists(path) and sys.stdout is not None and not sys.stdout.isatty():
                use_stdout_inode = os.fstat(sys.stdout.fileno())
                file_stat = os.stat(path)
                self._stdout_is_log_file = use_stdout_inode.st_ino == file_stat.st_ino
        except (OSError, ValueError, AttributeError):
            self._stdout_is_log_file = False

    def _secrets(self):
        try:
            if callable(self.redact_provider):
                return [as_text(item) for item in self.redact_provider() if as_text(item)]
        except Exception:
            pass
        return []

    def redact(self, text):
        result = as_text(text)
        for secret in sorted(self._secrets(), key=len, reverse=True):
            if len(secret) >= MASK_MIN_LENGTH and secret in result:
                result = result.replace(secret, "***")
        # 兜底：常见密钥形态
        result = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", result)
        result = re.sub(r"(?i)(access_token|token|api[_-]?key|password|secret)=([^&\s,;]{4,})", r"\1=***", result)
        return result

    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self.path) <= MAX_LOG_BYTES:
                return
        except OSError:
            return
        for index in range(LOG_ROTATE_KEEP, 0, -1):
            source = "%s.%d" % (self.path, index - 1) if index > 1 else self.path
            target = "%s.%d" % (self.path, index)
            try:
                if os.path.exists(source):
                    os.replace(source, target)
            except OSError:
                pass

    def log(self, message):
        line = "[%s] %s" % (iso_now(), self.redact(message))
        line = line.replace("\r", " ").replace("\n", "\\n")
        if len(line) > 6000:
            line = line[:6000] + "...(truncated)"
        with self.lock:
            try:
                self._rotate_if_needed()
                ensure_dir(os.path.dirname(os.path.abspath(self.path)) or ".")
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
            if not self._stdout_is_log_file:
                try:
                    if sys.stdout is not None and not sys.stdout.closed:
                        sys.stdout.write(line + "\n")
                        sys.stdout.flush()
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def normalize_config(raw):
    """把任意来源的 config 归一化为契约形状（保留未知键，便于 UI 扩展）。"""
    cfg = deep_merge(DEFAULT_CONFIG, raw if is_object(raw) else {})
    defaults = DEFAULT_CONFIG

    cfg["timeZone"] = first_non_blank(cfg.get("timeZone"), defaults["timeZone"])
    cfg["containerName"] = first_non_blank(cfg.get("containerName"), defaults["containerName"])
    cfg["image"] = first_non_blank(cfg.get("image"), defaults["image"])
    cfg["webuiPort"] = clamp_int(cfg.get("webuiPort"), 1, 65535, defaults["webuiPort"])
    cfg["onebotPort"] = clamp_int(cfg.get("onebotPort"), 1, 65535, defaults["onebotPort"])
    cfg["onebotToken"] = as_text(cfg.get("onebotToken")).strip()
    cfg["bridgeToken"] = as_text(cfg.get("bridgeToken")).strip()
    cfg["autoStartBridge"] = to_bool(cfg.get("autoStartBridge"), defaults["autoStartBridge"])
    cfg["webuiHost"] = first_non_blank(cfg.get("webuiHost"), defaults["webuiHost"])
    cfg["onebotHost"] = first_non_blank(cfg.get("onebotHost"), defaults["onebotHost"])

    source_deepseek = cfg.get("deepseek") if is_object(cfg.get("deepseek")) else {}
    deepseek = deep_merge(defaults["deepseek"], source_deepseek)
    deepseek["baseUrl"] = first_non_blank(deepseek.get("baseUrl"), defaults["deepseek"]["baseUrl"])
    deepseek["apiKey"] = as_text(deepseek.get("apiKey")).strip()
    deepseek["model"] = first_non_blank(deepseek.get("model"), defaults["deepseek"]["model"])
    deepseek["systemPrompt"] = as_text(deepseek.get("systemPrompt"))
    if not deepseek["systemPrompt"].strip():
        deepseek["systemPrompt"] = defaults["deepseek"]["systemPrompt"]
    deepseek["temperature"] = clamp_float(deepseek.get("temperature"), 0.0, 2.0, defaults["deepseek"]["temperature"])
    deepseek["maxTokens"] = clamp_int(deepseek.get("maxTokens"), 1, 32768, defaults["deepseek"]["maxTokens"])
    deepseek["timeoutMs"] = clamp_int(deepseek.get("timeoutMs"), 1000, 600000, defaults["deepseek"]["timeoutMs"])
    effort = as_text(deepseek.get("effort")).strip().lower()
    deepseek["effort"] = effort if effort in ("low", "high", "max") else "high"
    effort_mode = as_text(deepseek.get("effortMode")).strip().lower()
    deepseek["effortMode"] = "fixed" if effort_mode == "fixed" else "adaptive"
    web = deep_merge(defaults["deepseek"]["webSearch"], deepseek.get("webSearch") if is_object(deepseek.get("webSearch")) else {})
    web["enabled"] = to_bool(web.get("enabled"), True)
    web["engine"] = first_non_blank(web.get("engine"), "bing")
    web["maxResults"] = clamp_int(web.get("maxResults"), 1, 10, 5)
    web["maxRounds"] = clamp_int(web.get("maxRounds"), 1, 5, 3)
    deepseek["webSearch"] = web
    cfg["deepseek"] = deepseek

    source_auto = cfg.get("autoReply") if is_object(cfg.get("autoReply")) else {}
    auto = deep_merge(defaults["autoReply"], source_auto)
    auto["enabled"] = to_bool(auto.get("enabled"), defaults["autoReply"]["enabled"])
    auto["privateEnabled"] = to_bool(auto.get("privateEnabled"), defaults["autoReply"]["privateEnabled"])
    auto["groupEnabled"] = to_bool(auto.get("groupEnabled"), defaults["autoReply"]["groupEnabled"])
    auto["requireAtInGroup"] = to_bool(auto.get("requireAtInGroup"), defaults["autoReply"]["requireAtInGroup"])
    group_reply_mode = as_text(auto.get("groupReplyMode")).strip().lower()
    auto["groupReplyMode"] = group_reply_mode if group_reply_mode in ("all", "at", "prob") else "at"
    auto["interjectProbability"] = clamp_int(auto.get("interjectProbability"), 0, 100, 30)
    auto["interjectMediaProbability"] = clamp_int(auto.get("interjectMediaProbability"), 0, 100, 80)
    auto["groupMergeMs"] = clamp_int(auto.get("groupMergeMs"), 0, 5000, 1200)
    auto["triggerPrefix"] = as_text(auto.get("triggerPrefix")).strip()
    auto["cooldownMs"] = clamp_int(auto.get("cooldownMs"), 0, 600000, defaults["autoReply"]["cooldownMs"])
    auto["maxHistory"] = clamp_int(auto.get("maxHistory"), 0, 100, defaults["autoReply"]["maxHistory"])
    cfg["autoReply"] = auto

    source_whitelist = cfg.get("whitelist") if is_object(cfg.get("whitelist")) else {}
    whitelist = deep_merge(defaults["whitelist"], source_whitelist)
    mode = first_non_blank(whitelist.get("mode")).lower()
    whitelist["mode"] = mode if mode in ("off", "whitelist", "blacklist") else defaults["whitelist"]["mode"]
    whitelist["users"] = unique_text_list(whitelist.get("users"))
    whitelist["groups"] = unique_text_list(whitelist.get("groups"))
    cfg["whitelist"] = whitelist

    source_skills = cfg.get("skills") if is_object(cfg.get("skills")) else {}
    skills_cfg = deep_merge(defaults["skills"], source_skills)
    mm = skills_cfg.get("multiMessage") if is_object(skills_cfg.get("multiMessage")) else {}
    mm["enabled"] = to_bool(mm.get("enabled"), True)
    mm["maxMessages"] = clamp_int(mm.get("maxMessages"), 1, 6, 4)
    skills_cfg["multiMessage"] = mm
    skills_cfg["autoAtInGroup"] = to_bool(skills_cfg.get("autoAtInGroup"), True)
    skills_cfg["quoteOnReply"] = to_bool(skills_cfg.get("quoteOnReply"), False)
    stickers = deep_merge(defaults["skills"]["stickers"], skills_cfg.get("stickers") if is_object(skills_cfg.get("stickers")) else {})
    stickers["dirHost"] = first_non_blank(stickers.get("dirHost"), defaults["skills"]["stickers"]["dirHost"])
    stickers["dirApp"] = first_non_blank(stickers.get("dirApp"), defaults["skills"]["stickers"]["dirApp"])
    skills_cfg["stickers"] = stickers
    images_cfg = deep_merge(defaults["skills"]["images"], skills_cfg.get("images") if is_object(skills_cfg.get("images")) else {})
    images_cfg["enabled"] = to_bool(images_cfg.get("enabled"), True)
    images_cfg["maxHistoryImages"] = clamp_int(images_cfg.get("maxHistoryImages"), 0, 20, 6)
    skills_cfg["images"] = images_cfg
    video_cfg = deep_merge(defaults["skills"]["video"], skills_cfg.get("video") if is_object(skills_cfg.get("video")) else {})
    video_cfg["enabled"] = to_bool(video_cfg.get("enabled"), True)
    video_cfg["frames"] = clamp_int(video_cfg.get("frames"), 1, 5, 3)
    skills_cfg["video"] = video_cfg
    group_reply_cfg = deep_merge(defaults["skills"]["groupReply"], skills_cfg.get("groupReply") if is_object(skills_cfg.get("groupReply")) else {})
    group_reply_cfg["quoteOthers"] = to_bool(group_reply_cfg.get("quoteOthers"), False)
    group_reply_cfg["botNames"] = unique_text_list(group_reply_cfg.get("botNames"))
    skills_cfg["groupReply"] = group_reply_cfg
    ga = deep_merge(defaults["skills"]["groupAdmin"], skills_cfg.get("groupAdmin") if is_object(skills_cfg.get("groupAdmin")) else {})
    ga["enabled"] = to_bool(ga.get("enabled"), False)
    ga["adminUsers"] = unique_text_list(ga.get("adminUsers"))
    skills_cfg["groupAdmin"] = ga
    cfg["skills"] = skills_cfg

    source_proactive = cfg.get("proactive") if is_object(cfg.get("proactive")) else {}
    proactive = deep_merge(defaults["proactive"], source_proactive)
    proactive["enabled"] = to_bool(proactive.get("enabled"), True)
    proactive["minSilenceMinutes"] = clamp_int(proactive.get("minSilenceMinutes"), 10, 10080, 240)
    proactive["maxPerDay"] = clamp_int(proactive.get("maxPerDay"), 0, 20, 2)
    proactive["minIntervalMinutes"] = clamp_int(proactive.get("minIntervalMinutes"), 10, 10080, 180)
    proactive["quietStart"] = clamp_int(proactive.get("quietStart"), 0, 23, 23)
    proactive["quietEnd"] = clamp_int(proactive.get("quietEnd"), 0, 23, 8)
    cfg["proactive"] = proactive

    source_profiles = cfg.get("profiles") if is_object(cfg.get("profiles")) else {}
    profiles = deep_merge(defaults["profiles"], source_profiles)
    profiles["enabled"] = to_bool(profiles.get("enabled"), True)
    profiles["summarizeEvery"] = clamp_int(profiles.get("summarizeEvery"), 5, 200, 15)
    profiles["minIntervalMinutes"] = clamp_int(profiles.get("minIntervalMinutes"), 1, 1440, 30)
    profiles["maxParticipants"] = clamp_int(profiles.get("maxParticipants"), 1, 30, 8)
    cfg["profiles"] = profiles

    source_perm = cfg.get("permissions") if is_object(cfg.get("permissions")) else {}
    perms = deep_merge(defaults["permissions"], source_perm)
    perms["ownerUsers"] = unique_text_list(perms.get("ownerUsers"))
    perms["sensitiveCommands"] = unique_text_list(perms.get("sensitiveCommands"))
    perms["highRelationshipCommands"] = unique_text_list(perms.get("highRelationshipCommands"))
    perms["highRelationshipThreshold"] = clamp_int(perms.get("highRelationshipThreshold"), 0, 100, 90)
    perms.pop("highAffinityCommands", None)
    perms.pop("highAffinityThreshold", None)
    cfg["permissions"] = perms

    source_asr = cfg.get("asr") if is_object(cfg.get("asr")) else {}
    asr = deep_merge(defaults["asr"], source_asr)
    asr["enabled"] = to_bool(asr.get("enabled"), True)
    asr["apiKey"] = as_text(asr.get("apiKey"))
    asr["endpoint"] = first_non_blank(asr.get("endpoint"), "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
    asr["model"] = first_non_blank(asr.get("model"), "paraformer-realtime-v2")
    asr["maxSeconds"] = clamp_int(asr.get("maxSeconds"), 1, 300, 60)
    asr["silkDecoder"] = first_non_blank(asr.get("silkDecoder"), "/opt/snowluma/bin/silk_decoder")
    cfg["asr"] = asr

    source_memory = cfg.get("memory") if is_object(cfg.get("memory")) else {}
    memory = deep_merge(defaults["memory"], source_memory)
    memory["enabled"] = to_bool(memory.get("enabled"), True)
    memory["extractEvery"] = clamp_int(memory.get("extractEvery"), 3, 100, 8)
    memory["extractCooldownMinutes"] = clamp_int(memory.get("extractCooldownMinutes"), 1, 1440, 10)
    memory["maxPromptItems"] = clamp_int(memory.get("maxPromptItems"), 1, 20, 6)
    memory["consolidateHour"] = clamp_int(memory.get("consolidateHour"), 0, 23, 4)
    cfg["memory"] = memory

    source_sleep = cfg.get("sleep") if is_object(cfg.get("sleep")) else {}
    sleep = deep_merge(defaults["sleep"], source_sleep)
    sleep["enabled"] = to_bool(sleep.get("enabled"), True)
    sleep["start"] = first_non_blank(sleep.get("start"), "23:00")
    sleep["end"] = first_non_blank(sleep.get("end"), "07:00")
    sleep["wakeThreshold"] = clamp_int(sleep.get("wakeThreshold"), 1, 100, 8)
    sleep["summaryEnabled"] = to_bool(sleep.get("summaryEnabled"), True)
    sleep["postWakeQuietMinutes"] = clamp_int(sleep.get("postWakeQuietMinutes"), 0, 1440, 30)
    sleep["flushMaxAttempts"] = clamp_int(sleep.get("flushMaxAttempts"), 1, 10, 3)
    sleep["ownerPrivateExempt"] = to_bool(sleep.get("ownerPrivateExempt"), True)
    sleep["maxBuffer"] = clamp_int(sleep.get("maxBuffer"), 10, 200, 40)
    cfg["sleep"] = sleep

    source_pricing = cfg.get("pricing") if is_object(cfg.get("pricing")) else {}
    pricing = deep_merge(defaults["pricing"], source_pricing)
    pricing["enabled"] = to_bool(pricing.get("enabled"), True)
    windows = []
    for item in (pricing.get("peakWindows") or []):
        text = as_text(item).strip()
        if re.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$", text):
            windows.append(text)
    pricing["peakWindows"] = windows or ["09:00-12:00", "14:00-18:00"]
    weekdays = []
    for item in (pricing.get("peakWeekdays") or []):
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 6 and value not in weekdays:
            weekdays.append(value)
    pricing["peakWeekdays"] = weekdays if weekdays else [0, 1, 2, 3, 4]
    holidays = []
    for item in (pricing.get("holidayDates") or []):
        text = as_text(item).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text) and text not in holidays:
            holidays.append(text)
    pricing["holidayDates"] = holidays
    pricing["ownerPrivateMax"] = to_bool(pricing.get("ownerPrivateMax"), True)
    try:
        pricing["peakReplyFactor"] = max(0.0, min(1.0, float(pricing.get("peakReplyFactor", 0.6))))
    except (TypeError, ValueError):
        pricing["peakReplyFactor"] = 0.6
    cfg["pricing"] = pricing

    source_mood = cfg.get("mood") if is_object(cfg.get("mood")) else {}
    mood = deep_merge(defaults["mood"], source_mood)
    mood["enabled"] = to_bool(mood.get("enabled"), True)
    mood["initial"] = clamp_int(mood.get("initial"), 0, 100, 55)
    mood["baseline"] = clamp_int(mood.get("baseline"), 0, 100, 50)
    try:
        mood["decayPerHour"] = max(0.0, min(20.0, float(mood.get("decayPerHour", 4))))
    except (TypeError, ValueError):
        mood["decayPerHour"] = 4.0
    mood["minScore"] = clamp_int(mood.get("minScore"), 0, 100, 5)
    mood["maxScore"] = clamp_int(mood.get("maxScore"), 0, 100, 95)
    mood["ruleCooldownSeconds"] = clamp_int(mood.get("ruleCooldownSeconds"), 10, 3600, 60)
    mood["affectProactive"] = to_bool(mood.get("affectProactive"), True)
    mood["proactiveLowThreshold"] = clamp_int(mood.get("proactiveLowThreshold"), 0, 100, 30)
    cfg["mood"] = mood

    source_voice = cfg.get("voice") if is_object(cfg.get("voice")) else {}
    voice = deep_merge(defaults["voice"], source_voice)
    voice["enabled"] = to_bool(voice.get("enabled"), True)
    voice["engine"] = first_non_blank(voice.get("engine"), "edge")
    voice["voice"] = first_non_blank(voice.get("voice"), "zh-CN-XiaoyiNeural")
    voice["rate"] = as_text(voice.get("rate")).strip()
    voice["pitch"] = as_text(voice.get("pitch")).strip()
    voice["maxChars"] = clamp_int(voice.get("maxChars"), 5, 200, 60)
    voice["maxPerDay"] = clamp_int(voice.get("maxPerDay"), 0, 30, 3)
    voice["autoMinFamiliarity"] = clamp_int(voice.get("autoMinFamiliarity"), 0, 100, 60)
    voice["requestFamiliarity"] = clamp_int(voice.get("requestFamiliarity"), 0, 100, 70)
    voice.pop("autoMinAffinity", None)
    voice.pop("requestAffinity", None)
    voice["moodMin"] = clamp_int(voice.get("moodMin"), 0, 100, 30)
    voice["edgeTtsBin"] = first_non_blank(voice.get("edgeTtsBin"), "/home/ubuntu/.local/bin/edge-tts")
    cfg["voice"] = voice
    return cfg


class ConfigStore:
    """<state-dir>/config.json 的只读视图（config.json 由 JS 侧唯一写盘）。

    - 启动时读取一次；运行期通过 mtime 监听 JS 侧改写并热加载（不写盘，避免互相覆盖）；
    - POST /config 只更新内存，服务即时生效；config.json 的落盘由 JS 侧负责；
    - bridgeToken 由启动参数 --token 校验，GET /config 不回传该字段。
    """

    def __init__(self, path, logger):
        self.path = path
        self.logger = logger
        self.lock = threading.RLock()
        self._mtime_ns = 0
        self._file_size = -1
        self.config = normalize_config(read_json_file(path))
        self._stamp_file()

    def _stamp_file(self):
        try:
            stat = os.stat(self.path)
            self._mtime_ns = stat.st_mtime_ns
            self._file_size = stat.st_size
        except OSError:
            self._mtime_ns = 0
            self._file_size = -1

    def reload_if_changed(self):
        """config.json 被 JS 侧改写时热加载；返回 (changed, old, new)。"""
        try:
            stat = os.stat(self.path)
        except OSError:
            return False, None, None
        with self.lock:
            if stat.st_mtime_ns == self._mtime_ns and stat.st_size == self._file_size:
                return False, None, None
            raw = read_json_file(self.path)
            self._stamp_file()
            if not raw:
                # JS 正在写盘（空文件/截断 JSON）：本轮保留内存配置，下次再试
                return False, None, None
            old = self.config
            self.config = normalize_config(raw)
            return True, deep_copy(old), deep_copy(self.config)

    def get(self):
        with self.lock:
            return deep_copy(self.config)

    def save_patch(self, patch):
        """仅更新内存（config.json 由 JS 侧落盘），供运行中热更新配置。"""
        if not is_object(patch):
            raise ValueError("config_patch_must_be_object")
        with self.lock:
            self.config = normalize_config(deep_merge(self.config, sanitize_config_patch(patch)))
            return deep_copy(self.config)

    def save_to_disk(self):
        """把当前内存配置原子写回 config.json（本部署没有 JS 侧写盘时使用）。"""
        with self.lock:
            data = deep_copy(self.config)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)
        self._stamp_file()
        return True

    def masked(self):
        """返回用于 GET /config 的脱敏副本 + 密钥是否已配置（bridgeToken 不回传）。"""
        with self.lock:
            config = deep_copy(self.config)
        flags = {
            "apiKeySet": bool(config["deepseek"].get("apiKey")),
            "onebotTokenSet": bool(config.get("onebotToken")),
            "bridgeTokenSet": bool(config.get("bridgeToken")),
        }
        config["deepseek"]["apiKey"] = mask_secret(config["deepseek"].get("apiKey"))
        config["onebotToken"] = mask_secret(config.get("onebotToken"))
        config.pop("bridgeToken", None)
        return config, flags


def sanitize_config_patch(patch):
    """POST /config 的清洗：掩码占位值（"********" 等）表示“不修改该密钥”。"""
    if not is_object(patch):
        return {}
    cleaned = deep_copy(patch)
    for key in ("onebotToken", "bridgeToken"):
        if key in cleaned and is_masked_placeholder(cleaned.get(key)):
            cleaned.pop(key, None)
    if is_object(cleaned.get("deepseek")) and is_masked_placeholder(cleaned["deepseek"].get("apiKey")):
        cleaned["deepseek"].pop("apiKey", None)
    for nested in ("deepseek", "autoReply", "whitelist"):
        if nested in cleaned and not is_object(cleaned.get(nested)):
            cleaned.pop(nested, None)
    return cleaned


def is_masked_placeholder(value):
    text = as_text(value).strip()
    return bool(text) and len(text) >= MASK_MIN_LENGTH and set(text) == {"*"}


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

def default_state():
    return {
        "version": VERSION,
        "firstStartAt": 0,
        "counters": {
            "processed": 0,
            "skipped": 0,
            "replied": 0,
            "errors": 0,
        },
        "lastError": "",
        "lastMessageAt": 0,
        "lastReplyAt": 0,
        "qq": {
            "selfId": "",
            "account": "",
            "nickname": "",
            "lastHeartbeatAt": 0,
            "lastEventAt": 0,
        },
        "bridge": {
            "wsPath": "",
            "lastConnectAt": 0,
            "reconnectCount": 0,
        },
        "container": {
            "pid": 0,
            "startedAt": 0,
            "lastAction": "",
            "lastActionAt": 0,
            "lastResult": "",
        },
        "autoReply": {
            "lastError": "",
            "lastSkipReason": "",
            "lastProcessedAt": 0,
            "lastReplyAt": 0,
        },
        "history": {},
        "seenMessageIds": [],
        "lastBotMessageIds": {},
        "memory": {},
        "effortOverrides": {},
        "proactiveChats": {},
        "proactive": {},
        "groupReplyModes": {},
        "groupInterjectChance": {},
        "groupContext": {},
        "memeLibrary": {},
        "sleepBuffers": {},
        "sleepCooldownUntil": {},
        "pokeReplies": {},
        "memeLookupState": {},
        "lastStickerSent": {},
        "pendingImage": {},
        "userProfiles": {},
        "groupParticipants": {},
        "mood": {},
        "voiceCounters": {},
    }


class StateStore:
    """<state-dir>/state.json 的读写；save_if_dirty 由后台线程定期调用。"""

    def __init__(self, path, logger):
        self.path = path
        self.logger = logger
        self.lock = threading.RLock()
        self.dirty = False
        self.data = default_state()
        loaded = read_json_file(path)
        if is_object(loaded):
            merged = deep_merge(default_state(), loaded)
            # 兼容：旧版本缺失的子结构补齐
            self.data = normalize_state(merged)
        chmod_quiet(path, 0o600)

    def snapshot(self):
        with self.lock:
            return deep_copy(self.data)

    def update(self, mutator):
        with self.lock:
            mutator(self.data)
            self.dirty = True

    def mark_dirty(self):
        with self.lock:
            self.dirty = True

    def counters(self):
        with self.lock:
            return dict(self.data.get("counters") or {})

    def incr(self, name, amount=1):
        with self.lock:
            counters = self.data.setdefault("counters", {})
            counters[name] = safe_int(counters.get(name), 0) + amount
            self.dirty = True
            return counters[name]

    def save_if_dirty(self, force=False):
        with self.lock:
            if not self.dirty and not force:
                return False
            payload = deep_copy(self.data)
            self.dirty = False
        try:
            atomic_write_json(self.path, payload, 0o600)
            return True
        except OSError as error:
            self.logger.log("write state failed: %s: %s" % (error.__class__.__name__, error))
            with self.lock:
                self.dirty = True
            return False


def normalize_state(state):
    defaults = default_state()
    result = deep_merge(defaults, state)
    result["version"] = VERSION
    result["firstStartAt"] = safe_int(result.get("firstStartAt"), 0) or now_ms()
    for key in ("counters", "qq", "bridge", "container", "autoReply", "history"):
        if not is_object(result.get(key)):
            result[key] = deep_copy(defaults[key])
    for key in ("processed", "skipped", "replied", "errors"):
        result["counters"][key] = max(0, safe_int(result["counters"].get(key), 0))
    history = {}
    for chat_key, messages in (result.get("history") or {}).items():
        if not isinstance(messages, list):
            continue
        cleaned = []
        for message in messages:
            if not is_object(message):
                continue
            role = first_non_blank(message.get("role"))
            content = as_text(message.get("content"))
            if role in ("user", "assistant") and content:
                entry = {"role": role, "content": content, "at": safe_int(message.get("at"), 0)}
                raw_images = message.get("images")
                if isinstance(raw_images, list):
                    images = [as_text(item) for item in raw_images if as_text(item)][:8]
                    if images:
                        entry["images"] = images
                cleaned.append(entry)
        if cleaned:
            history[as_text(chat_key)] = cleaned[-MAX_HISTORY_MESSAGES:]
    result["history"] = history
    seen_ids = result.get("seenMessageIds")
    if not isinstance(seen_ids, list):
        seen_ids = []
    result["seenMessageIds"] = [as_text(item) for item in seen_ids if as_text(item)][-MAX_SEEN_MESSAGE_IDS:]
    for extra_key in ("lastBotMessageIds", "memory", "effortOverrides", "proactiveChats", "proactive", "groupReplyModes", "groupInterjectChance", "groupContext", "memeLibrary", "sleepBuffers", "pokeReplies", "memeLookupState", "lastStickerSent", "pendingImage", "sleepCooldownUntil", "userProfiles", "groupParticipants", "mood", "voiceCounters"):
        if not is_object(result.get(extra_key)):
            result[extra_key] = deep_copy(defaults[extra_key])
    return result

# ---------------------------------------------------------------------------
# 裸 socket WebSocket 客户端（OneBot v11 正向 WS，复用 ref 网关实现并修正半帧/心跳问题）
# ---------------------------------------------------------------------------

class WebSocketClosed(Exception):
    def __init__(self, code=1006, reason="closed"):
        super().__init__("websocket closed: %s %s" % (code, reason))
        self.code = code
        self.reason = reason


class SimpleWebSocketClient:
    """最小 WebSocket 客户端：握手、掩码帧收发、分片重组、ping/pong、关闭帧。

    与 ref 实现的差异：
      - 读取采用“帧状态机”，socket 超时不会消费半帧导致后续错位（可安全用于带超时的轮询循环）；
      - 发送加锁，支持主循环与自动回复线程同时发 OneBot 动作；
      - 支持额外请求头（OneBot token 的 Authorization: Bearer）。
    """

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url, timeout=0.5, extra_headers=None):
        self.url = url
        self.timeout = timeout
        self.extra_headers = extra_headers if is_object(extra_headers) else {}
        self.sock = None
        self.buffer = b""
        self._frame = None
        self._message_chunks = []
        self._message_opcode = 0
        self.send_lock = threading.Lock()
        self.connected = False
        self.last_pong_at = 0

    # ---- 握手 ----

    def connect(self):
        parsed = urllib.parse.urlsplit(self.url)
        scheme = parsed.scheme.lower()
        if scheme not in ("ws", "wss"):
            raise RuntimeError("unsupported websocket scheme: %s" % scheme)
        host = parsed.hostname or ""
        if not host:
            raise RuntimeError("missing websocket host")
        port = parsed.port or (443 if scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = "%s?%s" % (path, parsed.query)

        raw_sock = socket.create_connection((host, port), timeout=10)
        try:
            if scheme == "wss":
                context = ssl.create_default_context()
                raw_sock = context.wrap_socket(raw_sock, server_hostname=host)
        except Exception:
            try:
                raw_sock.close()
            except OSError:
                pass
            raise
        raw_sock.settimeout(self.timeout)
        self.sock = raw_sock

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = host if parsed.port is None else "%s:%d" % (host, port)
        request_headers = [
            "GET %s HTTP/1.1" % path,
            "Host: %s" % host_header,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
            "User-Agent: OperitSnowLumaBridge/%s" % VERSION,
        ]
        for name, value in self.extra_headers.items():
            request_headers.append("%s: %s" % (name, value))
        request = "\r\n".join(request_headers) + "\r\n\r\n"
        self.sock.sendall(request.encode("utf-8"))

        response = self._recv_until_handshake(b"\r\n\r\n")
        header_text = response.decode("utf-8", errors="replace")
        lines = header_text.split("\r\n")
        status_line = lines[0] if lines else ""
        if " 101 " not in (" %s " % status_line) and not status_line.startswith("HTTP/1.1 101"):
            raise RuntimeError("websocket handshake failed: %s" % status_line)
        headers = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        expected_accept = base64.b64encode(
            hashlib.sha1((key + self.GUID).encode("utf-8")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected_accept:
            raise RuntimeError("invalid websocket accept header")
        self.connected = True

    def _recv_until_handshake(self, marker):
        deadline = time.time() + 10.0
        while marker not in self.buffer:
            if time.time() > deadline:
                raise RuntimeError("websocket handshake timeout")
            chunk = self.sock.recv(4096) if self.sock is not None else b""
            if not chunk:
                raise WebSocketClosed()
            self.buffer += chunk
        head, self.buffer = self.buffer.split(marker, 1)
        return head + marker

    # ---- 读取 ----

    def _recv_chunk(self):
        """返回新数据；socket 超时返回 b''（不视为关闭）。"""
        if self.sock is None:
            raise WebSocketClosed()
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return b""
        if not chunk:
            raise WebSocketClosed()
        return chunk

    def _fill(self, size):
        while len(self.buffer) < size:
            chunk = self._recv_chunk()
            if not chunk:
                return False
            self.buffer += chunk
        return True

    def read_frame(self):
        """读取一个完整帧；超时返回 None（帧状态保留，下次续读）；控制帧内部消化。"""
        while True:
            if self.sock is None:
                raise WebSocketClosed()
            frame = self._frame
            if frame is None:
                if not self._fill(2):
                    return None
                first = self.buffer[0]
                second = self.buffer[1]
                length_code = second & 0x7F
                ext_len = 2 if length_code == 126 else (8 if length_code == 127 else 0)
                mask_len = 4 if (second & 0x80) else 0
                header_len = 2 + ext_len + mask_len
                if not self._fill(header_len):
                    return None
                raw = self.buffer[:header_len]
                self.buffer = self.buffer[header_len:]
                length = length_code
                if ext_len == 2:
                    length = struct.unpack("!H", raw[2:4])[0]
                elif ext_len == 8:
                    length = struct.unpack("!Q", raw[2:10])[0]
                if length > MAX_WS_PAYLOAD:
                    raise RuntimeError("websocket frame too large: %d" % length)
                mask = raw[2 + ext_len:header_len] if mask_len else b""
                frame = {
                    "fin": bool(first & 0x80),
                    "opcode": first & 0x0F,
                    "length": length,
                    "mask": mask,
                    "payload": b"",
                }
                self._frame = frame
            while len(frame["payload"]) < frame["length"]:
                need = frame["length"] - len(frame["payload"])
                if self.buffer:
                    slice_size = min(need, len(self.buffer))
                    frame["payload"] += self.buffer[:slice_size]
                    self.buffer = self.buffer[slice_size:]
                    continue
                chunk = self._recv_chunk()
                if not chunk:
                    return None
                self.buffer += chunk
            self._frame = None
            payload = frame["payload"]
            mask = frame["mask"]
            if mask:
                payload = bytes(payload[index] ^ mask[index % 4] for index in range(len(payload)))
            opcode = frame["opcode"]
            if opcode == 0x9:  # ping -> pong
                self.send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                self.last_pong_at = now_ms()
                continue
            if opcode == 0x8:  # close
                code = 1000
                reason = ""
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                    reason = payload[2:].decode("utf-8", errors="replace")
                self.connected = False
                raise WebSocketClosed(code, reason)
            return {"opcode": opcode, "fin": frame["fin"], "payload": payload}

    def read_message(self):
        """读取一条完整消息（自动重组分片）；超时返回 None 且保留已收分片。"""
        while True:
            frame = self.read_frame()
            if frame is None:
                return None
            opcode = frame["opcode"]
            if opcode in (0x1, 0x2):
                self._message_opcode = opcode
                self._message_chunks = [frame["payload"]]
            elif opcode == 0x0 and self._message_chunks:
                self._message_chunks.append(frame["payload"])
            else:
                continue
            if frame["fin"]:
                payload = b"".join(self._message_chunks)
                opcode_result = self._message_opcode
                self._message_chunks = []
                self._message_opcode = 0
                return {"opcode": opcode_result, "payload": payload}

    # ---- 发送 ----

    def _send_frame(self, opcode, payload):
        if self.sock is None:
            raise WebSocketClosed()
        first = 0x80 | (opcode & 0x0F)
        length = len(payload)
        if length < 126:
            header = bytes([first, 0x80 | length])
        elif length < 65536:
            header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(payload[index] ^ mask[index % 4] for index in range(length))
        with self.send_lock:
            if self.sock is None:
                raise WebSocketClosed()
            self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def send_ping(self, payload=b""):
        self._send_frame(0x9, payload)

    def send_pong(self, payload=b""):
        try:
            self._send_frame(0xA, payload)
        except Exception:
            pass

    def close(self, code=1000, reason=""):
        try:
            payload = struct.pack("!H", code) + reason.encode("utf-8")
            self._send_frame(0x8, payload)
        except Exception:
            pass
        self.connected = False
        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# DeepSeek（OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------

def build_deepseek_url(base_url):
    """兼容 DeepSeek 官方两种 base_url 写法（docs 2026-10 实测）：
      https://api.deepseek.com            -> https://api.deepseek.com/v1/chat/completions
      https://api.deepseek.com/v1         -> https://api.deepseek.com/v1/chat/completions
      https://api.deepseek.com/v1/chat/completions -> 原样使用
    其它 OpenAI 兼容服务按“有 /v1 就拼 /chat/completions，否则补 /v1”处理。
    """
    base = as_text(base_url).strip().rstrip("/")
    if not base:
        return ""
    lowered = base.lower()
    if lowered.endswith("/chat/completions"):
        return base
    if "/v1" in lowered:
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def extract_deepseek_reply(parsed):
    if not is_object(parsed):
        raise ValueError("invalid_response")
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("empty_choices")
    first = choices[0] if is_object(choices[0]) else {}
    message = first.get("message") if is_object(first.get("message")) else {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if is_object(item):
                parts.append(as_text(item.get("text")))
            else:
                parts.append(as_text(item))
        content = "".join(parts)
    text = as_text(content).strip()
    # 注意：reasoning_content 是模型的思考过程，绝不能当回复发给用户。
    return text, as_text(parsed.get("model"))


_API_CALL_TIMES = []
_API_CALL_LOCK = threading.Lock()


def _allow_api_call(deepseek):
    max_per_minute = clamp_int(deepseek.get("maxCallsPerMinute"), 5, 300, 30)
    now = time.time()
    with _API_CALL_LOCK:
        while _API_CALL_TIMES and now - _API_CALL_TIMES[0] >= 60:
            _API_CALL_TIMES.pop(0)
        if len(_API_CALL_TIMES) >= max_per_minute:
            return False, max_per_minute
        _API_CALL_TIMES.append(now)
    return True, max_per_minute


def call_deepseek(config, messages, timeout_ms=None, tools=None, tool_executor=None, max_rounds=None, effort_override=None):
    """调用 DeepSeek（支持多模态 messages、reasoning_effort、工具调用循环）。"""
    deepseek = config.get("deepseek") if is_object(config.get("deepseek")) else {}
    started = now_ms()
    url = build_deepseek_url(deepseek.get("baseUrl"))
    api_key = as_text(deepseek.get("apiKey")).strip()
    model = first_non_blank(deepseek.get("model"), "deepseek-flash")
    if not url:
        return {"ok": False, "error": "deepseek baseUrl 未配置", "elapsedMs": 0, "model": model}
    allowed, max_per_minute = _allow_api_call(deepseek)
    if not allowed:
        return {"ok": False, "error": "DeepSeek 调用频率保护：每分钟超过 %d 次，本条暂不调用" % max_per_minute,
                "elapsedMs": 0, "model": model}
    timeout_s = max(1.0, (safe_int(timeout_ms, 0) or safe_int(deepseek.get("timeoutMs"), 60000)) / 1000.0)
    effort = as_text(effort_override or deepseek.get("effort")).strip().lower()
    thinking_mode = as_text(deepseek.get("thinkingMode") or "auto").strip().lower()
    if thinking_mode == "disabled":
        thinking_enabled = False
    elif thinking_mode == "enabled":
        thinking_enabled = True
    else:
        thinking_enabled = effort in ("high", "max")
    body = {
        "model": model,
        "messages": messages,
        "temperature": clamp_float(deepseek.get("temperature"), 0.0, 2.0, 0.7),
        "max_tokens": clamp_int(deepseek.get("maxTokens"), 1, 32768, 2048),
        "stream": False,
    }
    if thinking_enabled:
        body["thinking"] = {"type": "enabled"}
        if effort in ("low", "high", "max"):
            body["reasoning_effort"] = effort
    else:
        body["thinking"] = {"type": "disabled"}
    tools = [item for item in (tools or []) if is_object(item)]
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    web = deepseek.get("webSearch") if is_object(deepseek.get("webSearch")) else {}
    rounds = max(1, min(5, safe_int(max_rounds or web.get("maxRounds"), 3)))
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "OperitSnowLumaBridge/%s" % VERSION,
    }
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    usage_total = {}
    tool_rounds = 0
    tool_trace = []
    empty_retries = 0
    last_reply_model = model

    def request_once(payload_body):
        payload = json.dumps(payload_body, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for attempt in range(2):
            try:
                request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=timeout_s) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw.strip() else {}
                if not is_object(parsed):
                    raise ValueError("invalid_response")
                return parsed, ""
            except urllib.error.HTTPError as error:
                detail = ""
                try:
                    detail = error.read().decode("utf-8", errors="replace").strip()
                except Exception:
                    detail = ""
                last_error = "DeepSeek HTTP %d: %s" % (error.code, (detail or error.reason or "")[:300])
                if error.code >= 500 and attempt == 0:
                    time.sleep(0.8)
                    continue
                return None, last_error
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as error:
                last_error = "DeepSeek 网络错误: %s: %s" % (error.__class__.__name__, error)
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                return None, last_error
            except ValueError as error:
                return None, "DeepSeek 响应解析失败: %s" % error
            except Exception as error:
                return None, "DeepSeek 调用失败: %s: %s" % (error.__class__.__name__, error)
        return None, last_error or "DeepSeek 调用失败"

    while True:
        parsed, error = request_once(body)
        if parsed is None:
            return {"ok": False, "error": error, "elapsedMs": now_ms() - started, "model": model,
                    "toolRounds": tool_rounds}
        choices = parsed.get("choices") if isinstance(parsed.get("choices"), list) else []
        message = (choices[0].get("message") if choices and is_object(choices[0]) else {}) if choices else {}
        message = message if is_object(message) else {}
        usage = parsed.get("usage") if is_object(parsed.get("usage")) else {}
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                usage_total[key] = usage_total.get(key, 0) + value
        for detail_key in ("completion_tokens_details", "output_tokens_details"):
            detail = usage.get(detail_key) if is_object(usage.get(detail_key)) else {}
            reasoning_tokens = detail.get("reasoning_tokens") if is_object(detail) else None
            if isinstance(reasoning_tokens, (int, float)):
                usage_total["reasoning_tokens"] = usage_total.get("reasoning_tokens", 0) + int(reasoning_tokens)
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if tool_calls and tool_executor is not None and tool_rounds < rounds:
            assistant_message = {"role": "assistant", "content": message.get("content") or ""}
            reasoning = as_text(message.get("reasoning_content")).strip()
            if reasoning:
                assistant_message["reasoning_content"] = reasoning
            assistant_calls = []
            for call in tool_calls:
                if not is_object(call):
                    continue
                function = call.get("function") if is_object(call.get("function")) else {}
                assistant_calls.append({
                    "id": as_text(call.get("id")),
                    "type": "function",
                    "function": {
                        "name": as_text(function.get("name")),
                        "arguments": as_text(function.get("arguments")) or "{}",
                    },
                })
            assistant_message["tool_calls"] = assistant_calls
            messages.append(assistant_message)
            for call in assistant_calls:
                function_name = as_text(call.get("function", {}).get("name"))
                raw_arguments = as_text(call.get("function", {}).get("arguments")) or "{}"
                try:
                    arguments = json.loads(raw_arguments)
                    if not is_object(arguments):
                        arguments = {}
                except ValueError:
                    arguments = {}
                try:
                    tool_trace.append("%s(%s)" % (function_name, json.dumps(arguments, ensure_ascii=False)[:200]))
                except Exception:
                    pass
                tool_images = []
                try:
                    tool_result = tool_executor(function_name, arguments)
                except Exception as tool_error:
                    tool_result = "工具执行失败: %s: %s" % (tool_error.__class__.__name__, tool_error)
                if isinstance(tool_result, dict):
                    result_text = as_text(tool_result.get("text"))
                    tool_images = [as_text(item) for item in (tool_result.get("images") or []) if as_text(item)]
                else:
                    result_text = as_text(tool_result)
                messages.append({"role": "tool", "tool_call_id": as_text(call.get("id")),
                                 "name": function_name, "content": result_text[:4000]})
                if tool_images:
                    try:
                        content = skills_ext.build_user_content(
                            "[video_zoom 放大图，结合上面的格号/时间看]", tool_images[:2])
                        messages.append({"role": "user", "content": content})
                    except Exception as image_error:
                        messages.append({"role": "user", "content": "放大图读取失败: %s" % image_error})
            tool_rounds += 1
            continue
        reply, response_model = extract_deepseek_reply(parsed)
        last_reply_model = response_model or model
        if not reply and not tool_calls and empty_retries < 1 and body.get("max_tokens", 0) < 8192:
            # 思考 token 把输出预算吃光时，content 会为空但 reasoning_content 有内容；
            # 不要把 reasoning 当回复，而是加大 max_tokens 重试一次。
            body["max_tokens"] = min(8192, max(2048, int(body.get("max_tokens", 2048)) * 2))
            empty_retries += 1
            continue
        elapsed = now_ms() - started
        if not reply:
            return {"ok": False, "error": "DeepSeek 返回空回复（思考 token 可能占满输出预算）",
                    "elapsedMs": elapsed, "model": last_reply_model,
                    "usage": usage_total, "toolRounds": tool_rounds, "toolTrace": tool_trace}
        return {"ok": True, "reply": reply, "model": last_reply_model,
                "elapsedMs": elapsed, "usage": usage_total, "toolRounds": tool_rounds,
                "toolTrace": tool_trace}


# ---------------------------------------------------------------------------
# OneBot v11 消息事件解析
# ---------------------------------------------------------------------------

CQ_CODE_RE = re.compile(r"\[CQ:([A-Za-z0-9_.\-]+)((?:,[^\]]*)?)\]")


def unescape_cq_text(text):
    return (as_text(text)
            .replace("&#91;", "[")
            .replace("&#93;", "]")
            .replace("&#44;", ",")
            .replace("&amp;", "&"))


def parse_cq_string(raw):
    """解析 OneBot v11 CQ 码字符串，返回 (纯文本, at 列表, 媒体数)。"""
    text = as_text(raw)
    ats = []
    media = 0
    parts = []
    last = 0
    for match in CQ_CODE_RE.finditer(text):
        parts.append(text[last:match.start()])
        last = match.end()
        cq_type = match.group(1)
        params_text = (match.group(2) or "").lstrip(",")
        params = {}
        for item in params_text.split(","):
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            params[name.strip()] = unescape_cq_text(value.strip())
        if cq_type == "at":
            qq = first_non_blank(params.get("qq"))
            if qq:
                ats.append(qq)
        elif cq_type in ("image", "mface", "record", "video", "file"):
            media += 1
    parts.append(text[last:])
    return unescape_cq_text("".join(parts)).strip(), ats, media


def parse_share_card(raw):
    """解析 QQ 小程序/链接卡片（json/xml），返回 (title, desc, url)。"""
    text = as_text(raw).strip()
    if not text:
        return "", "", ""
    title = desc = url = ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
            prompt = as_text(data.get("prompt"))
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            detail = meta.get("detail_1") if isinstance(meta.get("detail_1"), dict) else {}
            if not detail:
                for value in meta.values():
                    if isinstance(value, dict) and ("desc" in value or "qqdocurl" in value):
                        detail = value
                        break
            title = as_text(detail.get("title")) or prompt
            desc = as_text(detail.get("desc")) or prompt
            url = as_text(detail.get("qqdocurl")) or as_text(detail.get("url"))
            return title[:100], desc[:200], url[:500]
        except Exception:
            pass
    title_match = re.search(r"<title>([^<]{1,200})</title>", text, re.I)
    summary_match = re.search(r"<summary>([^<]{1,300})</summary>", text, re.I)
    url_match = re.search(r"<url>([^<]{1,500})</url>", text, re.I)
    if title_match:
        title = title_match.group(1)
    if summary_match:
        desc = summary_match.group(1)
    if url_match:
        url = url_match.group(1)
    return title.strip()[:100], desc.strip()[:200], url.strip()[:500]


def normalize_onebot_message(payload):
    """把 OneBot v11 message 事件归一化为内部结构；不支持的事件返回 None。"""
    if not is_object(payload):
        return None
    if first_non_blank(payload.get("post_type")) != "message":
        return None
    message_type = first_non_blank(payload.get("message_type"))
    group_id = first_non_blank(payload.get("group_id"))
    if not message_type:
        message_type = "group" if group_id else "private"
    if message_type not in ("private", "group"):
        return None
    sender = payload.get("sender") if is_object(payload.get("sender")) else {}
    user_id = first_non_blank(payload.get("user_id"), sender.get("user_id"))
    if not user_id:
        return None
    self_id = first_non_blank(payload.get("self_id"))
    message = payload.get("message")
    text = ""
    at_qqs = []
    face_ids = []
    image_types = []
    forward_ids = []
    media = 0
    share_count = 0
    share_url = ""
    if isinstance(message, list):
        text_parts = []
        for segment in message:
            if not is_object(segment):
                continue
            segment_type = first_non_blank(segment.get("type"))
            data = segment.get("data") if is_object(segment.get("data")) else {}
            if segment_type == "text":
                text_parts.append(as_text(data.get("text")))
            elif segment_type == "at":
                qq = first_non_blank(data.get("qq"))
                if qq:
                    at_qqs.append(qq)
                    if self_id and str(qq) == str(self_id):
                        text_parts.append("@你 ")
                    else:
                        text_parts.append("@%s " % qq)
            elif segment_type == "face":
                face_id = first_non_blank(data.get("id"))
                if face_id:
                    face_ids.append(face_id)
                    text_parts.append("[表情:%s]" % face_id)
            elif segment_type in ("image", "mface"):
                media += 1
                if segment_type == "mface":
                    image_types.append({"sub_type": -1, "summary": "[商城表情]"})
                else:
                    try:
                        sub_type = int(data.get("sub_type") or 0)
                    except (TypeError, ValueError):
                        sub_type = 0
                    image_types.append({"sub_type": sub_type, "summary": as_text(data.get("summary"))})
            elif segment_type == "forward":
                forward_id = first_non_blank(data.get("id"))
                if forward_id:
                    forward_ids.append(forward_id)
                    text_parts.append("[合并转发]")
            elif segment_type in ("record", "video", "file"):
                media += 1
            elif segment_type in ("json", "xml"):
                card = data.get("data") if isinstance(data.get("data"), str) else data.get("xml")
                title, desc, url = parse_share_card(card)
                label = desc or title
                if label:
                    text_parts.append("[分享：" + label + ((" 链接：" + url) if url else "") + "]")
                    share_count += 1
                    share_url = url
        text = "".join(text_parts).strip()
    else:
        raw_message = message if isinstance(message, str) else first_non_blank(payload.get("raw_message"))
        text, at_qqs, media = parse_cq_string(raw_message)
    if not text.strip() and media:
        text = "[图片]"
    # OneBot often delivers Bilibili/Douyin shares as plain text links rather
    # than a share segment.  Treat those links as shares so enrich_share runs.
    if not share_url:
        for found in re.finditer(r"https?://[^\s<>\"'）】]+", text):
            candidate = found.group(0).rstrip("，。！？、；：）】")
            if re.search(r"b23\.tv|bilibili\.com|bili2233\.cn|douyin\.com|iesdouyin\.com", candidate, re.I):
                share_url = candidate
                share_count = max(1, share_count)
                break
    return {
        "message_id": first_non_blank(payload.get("message_id")),
        "self_id": self_id,
        "message_type": message_type,
        "user_id": user_id,
        "group_id": group_id,
        "text": text.strip(),
        "at_qqs": at_qqs,
        "face_ids": face_ids,
        "image_types": image_types,
        "forward_ids": forward_ids,
        "media_count": media,
        "share_count": share_count,
        "share_url": share_url,
        "sender_name": first_non_blank(sender.get("card"), sender.get("nickname")),
        "time": safe_int(payload.get("time"), 0),
        "raw": payload,
    }


# ---------------------------------------------------------------------------
# OneBot 正向 WebSocket 桥
# ---------------------------------------------------------------------------

class OneBotError(Exception):
    pass


WS_PATH_CANDIDATES = ["/", "/onebot/v11/ws", "/onebot/v11", "/ws"]


class OneBotBridge:
    """OneBot v11 正向 WS 客户端：连接/重连、心跳监控、动作请求-响应（echo）。"""

    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.ws = None
        self.ws_connected = False
        self.connected_at = 0
        self.last_connect_at = 0
        self.last_packet_at = 0
        self.last_heartbeat_at = 0
        self.heartbeat_interval_ms = 30000
        self.reconnect_count = 0
        self.last_error = ""
        self.last_close_code = 0
        self.last_close_reason = ""
        self.self_id = ""
        self.ws_path = ""
        self.last_url = ""
        self._restart_requested = False
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._login_info_at = 0
        self._login_info_inflight = False

    # ---- 生命周期 ----

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="onebot_bridge", daemon=True)
        self.thread.start()

    def stop(self, timeout=3.0):
        self.stop_event.set()
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.1, timeout))
        self.thread = None
        self._fail_pending("桥接服务已停止")

    def request_restart(self):
        with self.lock:
            self.last_error = ""
            self._restart_requested = True
        self.service.log("收到 restart-bridge 请求，重置 OneBot 连接")
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def is_connected(self):
        return bool(self.ws_connected)

    def current_self_id(self):
        return first_non_blank(self.self_id)

    # ---- 连接参数 ----

    def _sanitize_uri(self, url):
        return re.sub(r"(access_token=)[^&\s]+", r"\1***", as_text(url))

    def _build_url(self, host, port, token, path):
        url = "ws://%s:%d%s" % (host, port, path)
        if token:
            separator = "&" if "?" in url else "?"
            url = url + separator + "access_token=" + urllib.parse.quote(token, safe="")
        return url

    def _extra_headers(self, token):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer %s" % token
        return headers

    def _idle_timeout_ms(self):
        if self.heartbeat_interval_ms > 0:
            return max(90000, int(self.heartbeat_interval_ms * 3.0))
        return 300000

    # ---- 主循环 ----

    def _run(self):
        backoff_s = 1.0
        path_index = 0
        first_attempt = True
        while not self.stop_event.is_set():
            config = self.service.config_store.get()
            host = first_non_blank(config.get("onebotHost"), "127.0.0.1")
            port = clamp_int(config.get("onebotPort"), 1, 65535, 3001)
            token = as_text(config.get("onebotToken")).strip()
            candidates = list(WS_PATH_CANDIDATES)
            remembered = first_non_blank(self.ws_path)
            if remembered:
                candidates = [remembered] + [item for item in candidates if item != remembered]
            path = candidates[path_index % len(candidates)]
            url = self._build_url(host, port, token, path)
            handshake_failed = False
            try:
                with self.lock:
                    self.last_url = self._sanitize_uri(url)
                if first_attempt:
                    self.service.log("OneBot 连接中: %s" % self._sanitize_uri(url))
                    first_attempt = False
                ws = SimpleWebSocketClient(url, timeout=0.5, extra_headers=self._extra_headers(token))
                ws.connect()
                with self.lock:
                    self.ws = ws
                    self.ws_connected = True
                    self.connected_at = now_ms()
                    self.last_connect_at = now_ms()
                    self.last_packet_at = now_ms()
                    self.ws_path = path
                    self.last_error = ""
                    self.last_close_code = 0
                    self.last_close_reason = ""
                backoff_s = 1.0
                self.service.log("OneBot WebSocket 已连接: path=%s self_id=%s" % (path, self.self_id or "?"))
                self.service.on_bridge_connected()
                self._maybe_fetch_login_info(force=True)
                self._read_loop(ws)
            except WebSocketClosed as closed:
                with self.lock:
                    self.last_close_code = closed.code
                    self.last_close_reason = closed.reason
                    if not self.stop_event.is_set():
                        self.last_error = "WebSocketClosed: %s %s" % (closed.code, closed.reason)
            except Exception as error:
                message = "%s: %s" % (error.__class__.__name__, error)
                if "handshake failed" in message or "invalid websocket accept" in message:
                    handshake_failed = True
                with self.lock:
                    if not self.stop_event.is_set():
                        self.last_error = message
                self.service.log("OneBot 连接异常: %s" % message)
            finally:
                with self.lock:
                    self.ws = None
                    self.ws_connected = False
                self._fail_pending("OneBot 连接断开")

            if self.stop_event.is_set():
                break

            restarting = self._restart_requested
            if restarting:
                self._restart_requested = False
                backoff_s = 1.0
                path_index = 0
            elif handshake_failed:
                path_index = (path_index + 1) % len(candidates)
                backoff_s = 1.0
            with self.lock:
                self.reconnect_count += 1
                current_reconnect = self.reconnect_count
                current_error = self.last_error
            self.service.on_bridge_reconnect(current_reconnect, current_error)
            if restarting:
                continue
            self.stop_event.wait(backoff_s)
            backoff_s = min(30.0, max(1.0, backoff_s * 2))

        with self.lock:
            self.ws_connected = False
            self.ws = None
        self.service.on_bridge_stopped()

    def _read_loop(self, ws):
        while not self.stop_event.is_set():
            message = ws.read_message()
            if message is None:
                idle = now_ms() - self.last_packet_at if self.last_packet_at else 0
                if idle > self._idle_timeout_ms():
                    raise RuntimeError("OneBot 心跳超时（%dms 无数据）" % idle)
                self._maybe_fetch_login_info(force=False)
                continue
            if int(message.get("opcode") or 0) not in (0x1, 0x2):
                continue
            raw = message.get("payload") or b""
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                self.service.log("OneBot 收到非法 JSON: %s" % text[:200])
                continue
            if not is_object(payload):
                continue
            self.last_packet_at = now_ms()
            self._dispatch(payload)

    def _dispatch(self, payload):
        if "echo" in payload and ("retcode" in payload or "status" in payload or "data" in payload):
            self._resolve_pending(payload)
            return
        post_type = first_non_blank(payload.get("post_type"))
        if post_type == "meta_event":
            self._handle_meta_event(payload)
            return
        if post_type == "message":
            event = normalize_onebot_message(payload)
            if event:
                if event.get("self_id"):
                    self._note_self_id(event["self_id"], "")
                self.service.on_onebot_message(event)
            return
        if post_type in ("notice", "request"):
            notice_type = first_non_blank(payload.get("notice_type"))
            sub_type = first_non_blank(payload.get("sub_type"))
            if post_type == "notice" and (notice_type == "poke" or (notice_type == "notify" and sub_type == "poke")):
                self.service.auto_engine.handle_poke(payload)
            elif post_type == "notice" and notice_type in ("group_recall", "friend_recall"):
                self.service.auto_engine.handle_recall(payload)
            else:
                self.service.log("OneBot 事件 %s/%s（不参与自动回复）" % (post_type, notice_type or sub_type))

    def _handle_meta_event(self, payload):
        meta_type = first_non_blank(payload.get("meta_event_type"))
        self_id = first_non_blank(payload.get("self_id"))
        if meta_type == "heartbeat":
            self.last_heartbeat_at = now_ms()
            interval = payload.get("interval")
            if interval is None and is_object(payload.get("data")):
                interval = payload["data"].get("interval")
            interval = safe_int(interval, 0)
            if interval > 0:
                self.heartbeat_interval_ms = clamp_int(interval, 1000, 600000, 30000)
            status = payload.get("status") if is_object(payload.get("status")) else {}
            self.service.on_qq_heartbeat(self_id, status)
            self._maybe_fetch_login_info(force=False)
        elif meta_type == "lifecycle":
            self.service.on_qq_lifecycle(self_id, first_non_blank(payload.get("sub_type")))
        if self_id:
            self._note_self_id(self_id, "")

    def _note_self_id(self, self_id, nickname):
        self_id = first_non_blank(self_id)
        if not self_id:
            return
        changed = False
        with self.lock:
            if self.self_id != self_id:
                self.self_id = self_id
                changed = True
        if changed:
            self.service.on_qq_self_id(self_id, nickname)

    # ---- get_login_info（qq.loggedIn / selfId / account 的权威来源）----

    def _maybe_fetch_login_info(self, force=False):
        now = now_ms()
        with self.lock:
            if self._login_info_inflight:
                return
            if not force and self.self_id and (now - self._login_info_at) < 60000:
                return
            if not force and (now - self._login_info_at) < 15000:
                return
            self._login_info_inflight = True
            self._login_info_at = now
        thread = threading.Thread(target=self._fetch_login_info, name="onebot_login_info", daemon=True)
        thread.start()

    def _fetch_login_info(self):
        try:
            data = self.call_action("get_login_info", {}, timeout_ms=5000)
            user_id = first_non_blank(data.get("user_id"))
            nickname = first_non_blank(data.get("nickname"))
            if user_id:
                self._note_self_id(user_id, nickname)
                self.service.on_qq_login_info(user_id, nickname)
                self.service.log("get_login_info 成功: self_id=%s nickname=%s" % (user_id, nickname))
            else:
                self.service.log("get_login_info 返回缺少 user_id: %s" % json.dumps(data, ensure_ascii=False)[:200])
        except Exception as error:
            self.service.log("get_login_info 失败: %s" % error)
        finally:
            with self.lock:
                self._login_info_inflight = False

    # ---- 动作请求（send_private_msg / send_group_msg 等）----

    def call_action(self, action, params, timeout_ms=ACTION_TIMEOUT_MS):
        with self.lock:
            ws = self.ws
            connected = self.ws_connected
        if ws is None or not connected:
            raise OneBotError("OneBot WebSocket 未连接，无法发送动作: %s" % action)
        echo = "snowluma-%s" % uuid.uuid4().hex[:12]
        entry = {"event": threading.Event(), "response": None, "error": None}
        with self._pending_lock:
            self._pending[echo] = entry
        payload = {"action": action, "params": params or {}, "echo": echo}
        try:
            with self._send_lock:
                ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as error:
            with self._pending_lock:
                self._pending.pop(echo, None)
            raise OneBotError("发送 OneBot 动作失败: %s: %s" % (error.__class__.__name__, error))
        if not entry["event"].wait(max(1, safe_int(timeout_ms, ACTION_TIMEOUT_MS)) / 1000.0):
            with self._pending_lock:
                self._pending.pop(echo, None)
            raise OneBotError("OneBot 动作超时: %s" % action)
        if entry["error"]:
            raise OneBotError(entry["error"])
        return entry["response"] if is_object(entry["response"]) else {}

    def _resolve_pending(self, payload):
        echo = payload.get("echo")
        key = echo if isinstance(echo, str) else json.dumps(echo, sort_keys=True)
        with self._pending_lock:
            entry = self._pending.pop(key, None)
        if entry is None:
            return
        status = as_text(payload.get("status")).lower()
        retcode = safe_int(payload.get("retcode"), 0)
        if status == "failed" or retcode not in (0,):
            entry["error"] = "OneBot 动作失败: retcode=%s %s" % (
                retcode,
                first_non_blank(payload.get("wording"), payload.get("message"), payload.get("msg")),
            )
        else:
            entry["response"] = payload.get("data") if is_object(payload.get("data")) else payload
        entry["event"].set()

    def _fail_pending(self, reason):
        with self._pending_lock:
            entries = list(self._pending.values())
            self._pending.clear()
        for entry in entries:
            entry["error"] = reason
            entry["event"].set()

    # ---- 状态 ----

    def status(self):
        with self.lock:
            thread_alive = self.thread is not None and self.thread.is_alive()
            return {
                "running": bool(thread_alive),
                "wsConnected": bool(self.ws_connected),
                "connectedAt": self.connected_at,
                "lastConnectAt": self.last_connect_at,
                "lastPacketAt": self.last_packet_at,
                "lastHeartbeatAt": self.last_heartbeat_at,
                "heartbeatIntervalMs": self.heartbeat_interval_ms,
                "reconnectCount": self.reconnect_count,
                "lastError": self.last_error,
                "lastCloseCode": self.last_close_code,
                "lastCloseReason": self.last_close_reason,
                "url": self.last_url,
                "path": self.ws_path or "/",
                "selfId": self.self_id,
                "pendingActions": len(self._pending),
            }


# ---------------------------------------------------------------------------
# 白名单过滤
# ---------------------------------------------------------------------------

def evaluate_whitelist(whitelist, scene, user_id, group_id):
    """与 JS 侧 snowluma_common.ts 的白名单语义保持一致：
      - mode = "off"       : 不启用名单，所有人可触发（默认）
      - mode = "whitelist" : 仅 users/groups 中的对象可触发；名单为空 => 全部拒绝（防误触）
      - mode = "blacklist" : users/groups 中的对象被忽略
    返回 (allowed, reason)。
    """
    whitelist = whitelist if is_object(whitelist) else {}
    mode = first_non_blank(whitelist.get("mode")).lower() or "off"
    users = set(unique_text_list(whitelist.get("users")))
    groups = set(unique_text_list(whitelist.get("groups")))
    if mode == "off":
        return True, ""
    user_key = as_text(user_id).strip()
    group_key = as_text(group_id).strip()
    in_users = bool(user_key) and user_key in users
    in_groups = bool(group_key) and group_key in groups
    if mode == "blacklist":
        if in_users:
            return False, "user_blacklisted"
        if scene == "group" and in_groups:
            return False, "group_blacklisted"
        return True, ""
    if not users and not groups:
        return False, "whitelist_empty"
    if scene == "group":
        if in_groups or in_users:
            return True, ""
        return False, "group_not_whitelisted"
    return (True, "") if in_users else (False, "user_not_whitelisted")


# ---------------------------------------------------------------------------
# 自动回复引擎（单工作线程：过滤 -> DeepSeek -> OneBot 回发）
# ---------------------------------------------------------------------------

HELP_TEXT = skills_ext.HELP_TEXT


class AutoReplyEngine:
    def __init__(self, service):
        self.service = service
        self.queue = queue.Queue(maxsize=500)
        self.video_queue = queue.Queue(maxsize=20)
        self.stop_event = threading.Event()
        self.thread = None
        self.video_thread = None
        self.seen_ids = OrderedDict()
        self.last_processed_ms = {}
        self.repeat_state = {}
        self.last_incoming_ms = {}
        self.last_incoming_user = {}
        self.last_speaker = {}
        self.recent_speakers = {}
        self.recalled_ids = OrderedDict()
        self.dropped_count = 0
        state = service.state_store.data
        with service.state_store.lock:
            for item in state.get("seenMessageIds") or []:
                text = as_text(item)
                if text:
                    self.seen_ids[text] = True
            self.history = state.setdefault("history", {})

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="auto_reply", daemon=True)
        self.thread.start()
        self.video_thread = threading.Thread(target=self._video_run, name="video_jobs", daemon=True)
        self.video_thread.start()

    def stop(self, timeout=2.0):
        self.stop_event.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.1, timeout))
        self.thread = None
        try:
            self.video_queue.put_nowait(None)
        except queue.Full:
            pass
        video_thread = self.video_thread
        if video_thread is not None and video_thread.is_alive():
            video_thread.join(max(0.1, timeout))
        self.video_thread = None

    def enqueue(self, event):
        try:
            scene = "group" if as_text(event.get("message_type")) == "group" else "private"
            key = "group:%s" % as_text(event.get("group_id")) if scene == "group" else "private:%s" % as_text(event.get("user_id"))
            if key:
                self.last_incoming_ms[key] = now_ms()
                self.last_incoming_user[key] = as_text(event.get("user_id"))
                event["_previous_speaker"] = dict(self.last_speaker.get(key) or {})
                recent = self.recent_speakers.setdefault(key, [])
                event["_recent_speakers"] = [dict(item) for item in recent[-6:]]
                speaker = {"user": as_text(event.get("user_id")),
                           "name": as_text(event.get("sender_name")) or as_text(event.get("user_id")),
                           "message_id": as_text(event.get("message_id")),
                           "text": as_text(event.get("text"))[:80]}
                recent.append(speaker)
                self.last_speaker[key] = dict(speaker)
                if len(recent) > 12:
                    del recent[:-12]
        except Exception:
            pass
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(event)
            except queue.Full:
                pass
            self.dropped_count += 1

    def pending_count(self):
        return self.queue.qsize()

    # ---- worker ----

    def _run(self):
        while not self.stop_event.is_set():
            try:
                event = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                continue
            try:
                self._process(event)
            except Exception as error:
                self._record_error("处理消息异常: %s: %s" % (error.__class__.__name__, error))
                self.service.log("自动回复异常: %s\n%s" % (error, traceback.format_exc(limit=4)))

    def _record_social_event(self, chat_key, user_id, nickname, action, detail=""):
        try:
            with self.service.state_store.lock:
                events = self.service.state_store.data.setdefault("socialEvents", [])
                if not isinstance(events, list):
                    events = []
                events.append({"chat_key": as_text(chat_key), "user_id": as_text(user_id),
                               "nickname": as_text(nickname), "action": as_text(action),
                               "detail": as_text(detail)[:80], "at": now_ms()})
                self.service.state_store.data["socialEvents"] = events[-40:]
                self.service.state_store.dirty = True
        except Exception:
            pass

    def _video_run(self):
        while not self.stop_event.is_set():
            try:
                event = self.video_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                continue
            try:
                self._process(event, background_video=True)
            except Exception as error:
                self._record_error("视频后台处理异常: %s: %s" % (error.__class__.__name__, error))
                self.service.log("视频后台异常: %s\n%s" % (error, traceback.format_exc(limit=4)))

    @staticmethod
    def _is_video_event(event):
        raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
        message = raw.get("message")
        if isinstance(message, list):
            for segment in message:
                if isinstance(segment, dict) and as_text(segment.get("type")) == "video":
                    return True
        return bool(event.get("share_count") and as_text(event.get("share_url")))

    def _config(self):
        return self.service.config_store.get()

    def _context_cfg(self, config=None):
        config = config or self._config()
        behavior = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
        context = behavior.get("context") if isinstance(behavior.get("context"), dict) else {}
        return context if isinstance(context, dict) else {}

    def _context_mode(self, config=None):
        return as_text(self._context_cfg(config).get("mode")).lower() or "rolling"

    def _record_daily_user(self, event, scene, group_id, user_id, chat_key):
        conversation = getattr(self.service, "conversation", None)
        if conversation is None:
            return 0
        try:
            text = skills_ext.event_text(event)
            command_text = re.sub(r"@(?:你|\d+)\s*", "", text).strip()
            if command_text.startswith("/"):
                return 0
            media = []
            for item in (event.get("image_types") or []):
                if isinstance(item, dict):
                    media.append("image:%s" % safe_int(item.get("sub_type"), 0))
            if event.get("forward_ids"):
                media.append("forward")
            if skills_ext.extract_voice(event):
                media.append("voice")
            saved = as_text(text)
            if not saved and media:
                saved = "[" + "、".join(media) + "]"
            if not saved:
                return 0
            turn_id = conversation.record_user(
                chat_key, now_ms(), as_text(event.get("message_id")), as_text(user_id),
                as_text(event.get("sender_name")) or as_text(user_id), saved, media=media, source="auto")
            if turn_id:
                event["_conversation_turn_id"] = turn_id
            return turn_id
        except Exception as error:
            try:
                self.service.log("记录日级对话失败: %s" % error)
            except Exception:
                pass
            return 0

    def _record_daily_assistant(self, chat_key, text, source="auto"):
        conversation = getattr(self.service, "conversation", None)
        if conversation is None:
            return 0
        try:
            return conversation.record_assistant(chat_key, now_ms(), text, source=source)
        except Exception as error:
            try:
                self.service.log("记录日级回复失败: %s" % error)
            except Exception:
                pass
            return 0

    def _compact_daily_turn(self, row):
        text = as_text(row.get("text")).strip()
        if not text:
            return ""
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            config = self._config()
            tz = ZoneInfo(as_text(config.get("timeZone")) or "Asia/Shanghai")
            stamp = datetime.fromtimestamp(safe_int(row.get("at")) / 1000.0, tz).strftime("%H:%M")
        except Exception:
            stamp = ""
        sender = as_text(row.get("sender_name")) or as_text(row.get("sender_id")) or "群友"
        prefix = ("[" + stamp + "] ") if stamp else ""
        return "%s%s：%s" % (prefix, sender, text[:1200])

    def _purge_daily_turns(self, conversation, config):
        try:
            from datetime import datetime, timedelta
            context = self._context_cfg(config)
            retention = max(1, safe_int(context.get("retentionDays"), 2))
            today = conversation.day_key()
            cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=retention)).strftime("%Y-%m-%d")
            conversation.purge_before(cutoff)
        except Exception:
            pass

    def _maybe_compact_conversation(self, conversation, chat_key, context):
        try:
            threshold = max(10000, safe_int(context.get("compactThresholdTokens"), 700000))
            with self.service.state_store.lock:
                metrics = self.service.state_store.data.get("cacheMetrics") or {}
                item = metrics.get(chat_key) if isinstance(metrics.get(chat_key), dict) else {}
                last_tokens = safe_int(item.get("lastPromptTokens"), 0)
                if last_tokens < threshold:
                    return False
                max_id = conversation.max_turn_id(chat_key)
                keep = max(10, safe_int(context.get("keepTurnsAfterCompact"), 80))
                start_id = max(1, max_id - keep + 1)
                starts = self.service.state_store.data.setdefault("conversationStartIds", {})
                if not isinstance(starts, dict):
                    starts = {}
                    self.service.state_store.data["conversationStartIds"] = starts
                starts[chat_key] = start_id
                if isinstance(metrics.get(chat_key), dict):
                    metrics[chat_key]["lastPromptTokens"] = 0
                    metrics[chat_key]["compactedAt"] = now_ms()
                self.service.state_store.data["cacheMetrics"] = metrics
                self.service.state_store.dirty = True
            self.service.log("上下文压缩检查点: chat=%s start_id=%s keep=%s last_tokens=%s" % (
                chat_key, start_id, keep, last_tokens))
            return True
        except Exception as error:
            try:
                self.service.log("上下文压缩检查点失败: %s" % error)
            except Exception:
                pass
            return False

    def _build_daily_messages(self, config, chat_key, scene, user_id, current_turn_id, system_prompt):
        if self._context_mode(config) not in ("daily", "persistent"):
            return None
        conversation = getattr(self.service, "conversation", None)
        if conversation is None:
            return None
        try:
            context = self._context_cfg(config)
            self._maybe_compact_conversation(conversation, chat_key, context)
            with self.service.state_store.lock:
                starts = self.service.state_store.data.get("conversationStartIds") or {}
                start_id = safe_int(starts.get(chat_key), 0) if isinstance(starts, dict) else 0
            turns = conversation.list_turns(chat_key, min_id=start_id, exclude_id=current_turn_id)
            max_chars = max(10000, safe_int(context.get("maxInputTokens"), 900000) * 2)
            if conversation.estimate_chars(turns) > max_chars:
                self.service.log("持久上下文超过预算，回退滚动窗口: chat=%s rows=%d" % (chat_key, len(turns)))
                return None
            replay_images = to_bool(context.get("replayHistoryImages"), True)
            messages = [{"role": "system", "content": system_prompt}]
            for row in turns:
                role = as_text(row.get("role"))
                if role == "user":
                    prompt_text = as_text(row.get("prompt_text"))
                    if prompt_text:
                        images = []
                        if replay_images:
                            try:
                                images = [as_text(item) for item in json.loads(as_text(row.get("prompt_images_json")) or "[]") if as_text(item)]
                            except Exception:
                                images = []
                        content = skills_ext.build_user_content(prompt_text, images)
                    else:
                        content = self._compact_daily_turn(row)
                    if content:
                        messages.append({"role": "user", "content": content})
                elif role == "assistant":
                    content = as_text(row.get("text")).strip()
                    if content:
                        messages.append({"role": "assistant", "content": content})
            return messages
        except Exception as error:
            try:
                self.service.log("持久上下文组装失败，回退滚动窗口: %s" % error)
            except Exception:
                pass
            return None

    def _record_skip(self, reason):
        def mutate(data):
            data.setdefault("autoReply", {})["lastSkipReason"] = reason
        self.service.state_store.update(mutate)
        self.service.state_store.incr("skipped")

    def _record_error(self, message):
        def mutate(data):
            data["lastError"] = message
            data.setdefault("autoReply", {})["lastError"] = message
        self.service.state_store.update(mutate)
        self.service.state_store.incr("errors")
        self.service.log("自动回复错误: %s" % message)
        try:
            self.service.monitor.record_error("auto_reply", message)
        except Exception:
            pass

    def handle_poke(self, payload):
        def run():
            try:
                self._handle_poke(payload)
            except Exception as error:
                self.service.log("戳一戳处理失败: %s: %s" % (error.__class__.__name__, error))
        threading.Thread(target=run, name="snowluma-poke", daemon=True).start()

    def _handle_poke(self, payload):
        config = self._config()
        self_id = as_text(payload.get("self_id")) or as_text(self.service.state_store.snapshot().get("qq", {}).get("selfId"))
        group_id = as_text(payload.get("group_id"))
        user_id = as_text(payload.get("user_id") or payload.get("sender_id"))
        target_id = as_text(payload.get("target_id") or payload.get("target"))
        if not self_id or not user_id or str(user_id) == str(self_id):
            return
        if target_id and str(target_id) != str(self_id):
            return
        scene = "group" if group_id else "private"
        chat_key = "group:%s" % group_id if group_id else "private:%s" % user_id
        target = group_id if group_id else user_id
        self._record_social_event(chat_key, user_id, user_id, "poke")
        try:
            if self.service.sleep.is_sleeping():
                self.service.log("睡眠中忽略戳一戳: %s" % chat_key)
                return
        except Exception:
            pass
        actions = ((config.get("skills") or {}).get("actions") or {}) if isinstance(config.get("skills"), dict) else {}
        if not actions.get("pokeReply", True):
            return
        cooldown = max(5, safe_int(actions.get("pokeReplyCooldownSeconds"), 60)) * 1000
        max_per_day = max(1, safe_int(actions.get("pokeReplyMaxPerDay"), 6))
        now = now_ms()
        day = datetime.now().strftime("%Y-%m-%d")
        with self.service.state_store.lock:
            replies = self.service.state_store.data.setdefault("pokeReplies", {})
            bucket = replies.get(chat_key) if isinstance(replies.get(chat_key), dict) else {}
            record = bucket.get(user_id) if isinstance(bucket.get(user_id), dict) else {}
            if as_text(record.get("day")) != day:
                record = {"day": day, "count": 0, "last": 0}
            if now - safe_int(record.get("last"), 0) < cooldown or safe_int(record.get("count"), 0) >= max_per_day:
                return
            record["last"] = now
            record["count"] = safe_int(record.get("count"), 0) + 1
            bucket[user_id] = record
            replies[chat_key] = bucket
            self.service.state_store.dirty = True
        try:
            relation = self.service.relationships.state_for(scene, chat_key, user_id)
        except Exception:
            relation = {"familiarity": 50, "trust": 50, "tension": 0, "known": False}
        familiarity = safe_int(relation.get("familiarity"), 50)
        tension = safe_int(relation.get("tension"), 0)
        owner_ids = [as_text(item) for item in (((config.get("permissions") or {}).get("ownerUsers") or []))]
        # One social action per poke burst: text, poke back, or silence.
        # Relationship state shifts the weights; it never decides for the model.
        if scene == "group":
            if tension >= 65:
                silent, poke, text = 0.55, 0.30, 0.15
            elif familiarity >= 70:
                silent, poke, text = 0.20, 0.35, 0.45
            elif familiarity >= 40:
                silent, poke, text = 0.30, 0.40, 0.30
            else:
                silent, poke, text = 0.45, 0.35, 0.20
        elif user_id in owner_ids:
            silent, poke, text = 0.20, 0.15, 0.65
        else:
            silent, poke, text = 0.30, 0.30, 0.40
        roll = random.random()
        if roll < silent:
            self.service.log("戳一戳选择不回: chat=%s 用户=%s" % (chat_key, user_id))
            return
        if roll < silent + poke:
            try:
                poke_params = {"user_id": int(user_id) if str(user_id).isdigit() else user_id}
                if group_id:
                    poke_params["group_id"] = int(group_id) if str(group_id).isdigit() else group_id
                result = self.service.bridge.call_action("send_poke", poke_params, timeout_ms=10000)
                if result:
                    self.service.state_store.incr("replied")
                    self.service.log("戳一戳选择回戳: chat=%s 用户=%s" % (chat_key, user_id))
            except Exception as error:
                self.service.log("回戳失败: %s" % error)
            return
        # Text branch: no poke action, so the two decisions cannot contradict.
        nickname = user_id
        if group_id:
            try:
                info = self.service.bridge.call_action("get_group_member_info", {"group_id": group_id, "user_id": user_id})
                if isinstance(info, dict):
                    nickname = as_text(info.get("card")) or as_text(info.get("nickname")) or user_id
            except Exception:
                pass
        try:
            system_prompt = skills_ext.build_system_prompt(self.service, config, chat_key, scene, user_id=user_id)
            system_prompt += ("\n\n【被戳】%s 刚刚戳了你一下。这是一次社交性戳，不是系统事件。"
                              "你选择回应一句，可以吐槽、回击、装烦、只回一个“？”；根据你们的关系决定力度，"
                              "不要解释自己，不要长篇，也不要用“戳一戳事件”这种系统说法。") % nickname
            user_content = "[戳一戳] %s 戳了你一下" % nickname
            effort = "max" if (scene == "private" and user_id in owner_ids) else "low"
            result = call_deepseek(config, [{"role": "system", "content": system_prompt},
                                            {"role": "user", "content": user_content}], effort_override=effort)
            reply = as_text(result.get("reply")).strip() if result.get("ok") else ""
            reply = skills_ext.extract_final_envelope(reply)
            reply = skills_ext.strip_reasoning_leak(reply, drop_foreign_meta=True)
            if not reply or "[[silent]]" in reply:
                self.service.log("戳一戳文本分支选择沉默: chat=%s 用户=%s" % (chat_key, user_id))
                return
            reply = reply[:200]
            sent = self.service.send_message_now(scene, target, reply)
            if sent.get("ok"):
                self.service.state_store.incr("processed")
                self.service.state_store.incr("replied")
                self.service.log("已回应戳一戳: chat=%s 用户=%s" % (chat_key, nickname))
                try:
                    self._append_history(chat_key, "user", "[戳一戳] " + nickname + " 戳了你一下", 20)
                    self._append_history(chat_key, "assistant", reply, 20)
                    self._record_daily_assistant(chat_key, reply, source="poke")
                except Exception:
                    pass
        except Exception as error:
            self.service.log("戳一戳回复失败: %s" % error)

    def is_recalled(self, message_id):
        message_id = as_text(message_id)
        if not message_id:
            return False
        return message_id in self.recalled_ids

    def handle_recall(self, payload):
        message_id = as_text(payload.get("message_id") or payload.get("message_id"))
        group_id = as_text(payload.get("group_id"))
        chat_key = "group:%s" % group_id if group_id else ""
        if not message_id:
            self.service.log("撤回事件缺少 message_id")
            return
        self.recalled_ids[message_id] = now_ms()
        while len(self.recalled_ids) > 500:
            self.recalled_ids.popitem(last=False)
        if chat_key:
            with self.service.state_store.lock:
                data = self.service.state_store.data
                contexts = data.get("groupContext") or {}
                rows = contexts.get(chat_key) if isinstance(contexts.get(chat_key), list) else []
                kept = [row for row in rows if not (isinstance(row, dict) and as_text(row.get("message_id")) == message_id)]
                if len(kept) != len(rows):
                    kept.append({"line": "- [系统] 有一条消息已被撤回，不要根据它回复", "at": now_ms(), "message_id": ""})
                contexts[chat_key] = kept[-20:]
                data["groupContext"] = contexts
                self.service.state_store.dirty = True
            recent = self.recent_speakers.get(chat_key) or []
            self.recent_speakers[chat_key] = [item for item in recent if as_text(item.get("message_id")) != message_id]
            with self.service.state_store.lock:
                messages = self.history.get(chat_key) or []
                for index, entry in enumerate(list(messages)):
                    if as_text(entry.get("message_id")) != message_id:
                        continue
                    messages.pop(index)
                    if index < len(messages) and as_text(messages[index].get("role")) == "assistant":
                        messages.pop(index)
                    break
                self.service.state_store.data["history"] = self.history
                self.service.state_store.dirty = True
        self.service.log("消息已撤回: chat=%s message_id=%s" % (chat_key or "?", message_id))

    def _remember_video_context(self, chat_key, message_id, image_paths):
        for path in image_paths or []:
            path = as_text(path)
            if not path.endswith(".storyboard.png"):
                continue
            source = ""
            try:
                source = open(path + ".source", "r", encoding="utf-8").read().strip()
            except OSError:
                pass
            self.service.current_video = {"path": source, "sheet": path,
                                          "dir": os.path.dirname(path),
                                          "chat_key": chat_key, "message_id": message_id}
            return

    def _remember_group_context(self, chat_key, event, text):
        body = as_text(text).strip()
        if not body:
            return
        nickname = as_text(event.get("sender_name")) or as_text(event.get("user_id"))
        user_id = as_text(event.get("user_id"))
        try:
            quote = skills_ext.quoted_context(self.service, event)
        except Exception:
            quote = None
        if quote:
            body = "[引用 %s：%s] %s" % (as_text(quote.get("nickname")), as_text(quote.get("text")), body)
        line = "- %s(%s)：%s" % (nickname[:30], user_id, body[:200])
        def mutate(data):
            contexts = data.setdefault("groupContext", {})
            rows = contexts.get(chat_key) if isinstance(contexts.get(chat_key), list) else []
            rows.append({"line": line, "at": now_ms(), "message_id": as_text(event.get("message_id"))})
            contexts[chat_key] = rows[-20:]
        self.service.state_store.update(mutate)

    def _take_group_context(self, chat_key):
        with self.service.state_store.lock:
            contexts = self.service.state_store.data.get("groupContext") or {}
            rows = contexts.get(chat_key) if isinstance(contexts.get(chat_key), list) else []
            lines = [as_text(row.get("line")) for row in rows if isinstance(row, dict) and as_text(row.get("line"))]
            contexts[chat_key] = []
            self.service.state_store.data["groupContext"] = contexts
            self.service.state_store.dirty = True
        return "\n".join(lines[-20:])

    def _history_messages(self, chat_key, limit):
        with self.service.state_store.lock:
            messages = self.history.get(chat_key) or []
            if limit <= 0:
                return []
            rows = [dict(item) for item in messages[-limit:]]
        for item in rows:
            content = as_text(item.get("content"))
            marker = content.find("[对话场景]")
            if marker > 0:
                item["content"] = content[marker:]
        return rows

    def _append_history(self, chat_key, role, content, max_history, images=None, message_id=""):
        with self.service.state_store.lock:
            messages = self.history.setdefault(chat_key, [])
            entry = {"role": role, "content": content, "at": now_ms()}
            if message_id:
                entry["message_id"] = as_text(message_id)
            image_list = [as_text(item) for item in (images or []) if as_text(item)]
            if image_list:
                entry["images"] = image_list
            messages.append(entry)
            keep = max(2, max_history * 2)
            if len(messages) > keep:
                dropped = messages[:len(messages) - keep]
                del messages[:len(messages) - keep]
                if self._context_mode(self._config()) not in ("daily", "persistent"):
                    for old in dropped:
                        if isinstance(old.get("images"), list):
                            skills_ext.delete_images(old.get("images"))
            self.service.state_store.dirty = True

    def _clear_history(self, chat_key):
        with self.service.state_store.lock:
            self.history.pop(chat_key, None)
            self.service.state_store.dirty = True
        try:
            conversation = getattr(self.service, "conversation", None)
            if conversation is not None:
                conversation.clear_chat(chat_key)
            with self.service.state_store.lock:
                starts = self.service.state_store.data.get("conversationStartIds") or {}
                if isinstance(starts, dict):
                    starts.pop(chat_key, None)
                    self.service.state_store.dirty = True
        except Exception:
            pass
        self.recent_speakers.pop(chat_key, None)
        self.last_speaker.pop(chat_key, None)

    # ---- 单条消息处理 ----

    def _directed_at_bot(self, event, text, self_id, config):
        if first_non_blank(event.get("message_type")) == "private":
            return True
        at_qqs = [as_text(item).strip() for item in (event.get("at_qqs") or [])]
        if self_id and self_id in at_qqs:
            return True
        group_cfg = (_sk(config).get("groupReply") if isinstance(_sk(config).get("groupReply"), dict) else {})
        names = [as_text(self.service.state_store.snapshot().get("qq", {}).get("nickname"))]
        names.extend(as_text(item) for item in (group_cfg.get("botNames") or []))
        body = as_text(text)
        if any(name and name in body for name in names):
            return True
        try:
            quote = skills_ext.quoted_context(self.service, event)
        except Exception:
            quote = None
        if quote and self_id and as_text(quote.get("user_id")) == as_text(self_id):
            return True
        return False

    def _repeat_context_hint(self, event, text):
        if first_non_blank(event.get("message_type")) != "group":
            return ""
        face_ids = [as_text(item) for item in (event.get("face_ids") or []) if as_text(item)]
        body = as_text(text).strip()
        if face_ids and body == "[表情:%s]" % face_ids[0]:
            key = "face:" + face_ids[0]
        else:
            compact = re.sub(r"\s+", "", body)
            key = "text:" + compact if compact else ""
        if not key:
            return ""
        chat_key = "group:%s" % first_non_blank(event.get("group_id"))
        entry = self.repeat_state.get(chat_key)
        if not isinstance(entry, dict) or entry.get("key") != key:
            return ""
        repeated_at = safe_int(entry.get("repeated_at"), 0)
        if not repeated_at or int(time.time() * 1000) - repeated_at > 300 * 1000:
            return ""
        if event.get("at_qqs"):
            return ""
        return ("[复读说明] 你刚刚只是跟着大家复读了“%s”，这不是你的名字；这条大概率还在玩梗/说别人，"
                "不要认领成在叫你，也不要回“你叫我X”。" % body[:20])

    def _auto_repeat(self, event, config):
        skills_cfg = config.get("skills") if isinstance(config.get("skills"), dict) else {}
        block = skills_cfg.get("autoRepeat") if isinstance(skills_cfg.get("autoRepeat"), dict) else {}
        if not block.get("enabled", True):
            return False
        try:
            if self.service.sleep._sleep_window(self.service.sleep._now())[0]:
                return False
        except Exception:
            pass
        text = skills_ext.event_text(event).strip()
        if not text or text.startswith("/") or text == "[图片]" or event.get("at_qqs"):
            return False
        face_ids = [as_text(item) for item in (event.get("face_ids") or []) if as_text(item)]
        key = ""
        if face_ids and text == "[表情:%s]" % face_ids[0]:
            key = "face:" + face_ids[0]
        elif not face_ids and safe_int(event.get("media_count"), 0) == 0 and safe_int(event.get("share_count"), 0) == 0:
            compact = re.sub(r"\s+", "", text)
            if len(compact) <= clamp_int(block.get("maxLength"), 1, 30, 20):
                key = "text:" + compact
        if not key:
            return False
        chat_key = "group:%s" % first_non_blank(event.get("group_id"))
        target = first_non_blank(event.get("group_id"))
        user_id = first_non_blank(event.get("user_id"))
        now = int(time.time() * 1000)
        cooldown = clamp_int(block.get("cooldownSeconds"), 10, 600, 60) * 1000
        entry = self.repeat_state.get(chat_key) if isinstance(self.repeat_state.get(chat_key), dict) else {}
        if entry.get("key") != key or as_text(entry.get("user")) == as_text(user_id):
            entry = {"key": key, "user": user_id, "count": 1, "at": now, "repeated_at": int(entry.get("repeated_at") or 0)}
        else:
            entry["count"] = safe_int(entry.get("count"), 0) + 1
            entry["user"] = user_id
            entry["at"] = now
        self.repeat_state[chat_key] = entry
        if entry.get("repeated_chain") == key:
            # A repeat chain is broken by any different message (which replaces
            # this entry), not by a short timer.  This prevents a slow chain from
            # being re-repeated after two minutes.
            return "skip"
        if entry["count"] < 2:
            return False
        if now - int(entry.get("repeated_at") or 0) < cooldown:
            return False
        try:
            if key.startswith("face:"):
                result = skills_ext.send_segments(self.service, "group", target, [{"type": "face", "data": {"id": key[5:]}}])
            else:
                result = self.service.send_message_now("group", target, text)
        except Exception as error:
            self.service.log("自动复读失败: %s" % error)
            return False
        if result.get("ok"):
            entry["count"] = 0
            entry["repeated_at"] = now
            entry["repeated_chain"] = key
            self.repeat_state[chat_key] = entry
            self.service.state_store.incr("replied")
            self.service.log("自动复读: %s" % key[:60])
            self._record_daily_assistant(chat_key, text, source="auto_repeat")
            return True
        return False

    def _process(self, event, background_video=False):
        config = self._config()
        auto = config["autoReply"]
        scene = first_non_blank(event.get("message_type"))
        if scene not in ("private", "group"):
            self._record_skip("unsupported_scene")
            return
        message_id = first_non_blank(event.get("message_id"))
        if message_id and not background_video:
            if message_id in self.seen_ids:
                self._record_skip("duplicate")
                return
            self.seen_ids[message_id] = True
            while len(self.seen_ids) > MAX_SEEN_MESSAGE_IDS:
                self.seen_ids.popitem(last=False)
            with self.service.state_store.lock:
                self.service.state_store.data["seenMessageIds"] = list(self.seen_ids.keys())[-MAX_SEEN_MESSAGE_IDS:]
                self.service.state_store.dirty = True

        self.service.on_qq_message_event(event)
        self_id = first_non_blank(self.service.state_store.snapshot().get("qq", {}).get("selfId"), event.get("self_id"))
        user_id = first_non_blank(event.get("user_id"))
        was_addressed = False
        group_id = first_non_blank(event.get("group_id"))
        if self_id and user_id and str(user_id) == str(self_id):
            self._record_skip("bot_echo")
            return
        if not auto["enabled"]:
            self._record_skip("auto_reply_disabled")
            return
        if scene == "private" and not auto["privateEnabled"]:
            self._record_skip("private_disabled")
            return
        if scene == "group" and not auto["groupEnabled"]:
            self._record_skip("group_disabled")
            return
        allowed, reason = evaluate_whitelist(config["whitelist"], scene, user_id, group_id)
        if not allowed:
            self._record_skip(reason)
            return

        try:
            daily_chat_key = "group:%s" % group_id if scene == "group" else "private:%s" % user_id
            self._record_daily_user(event, scene, group_id, user_id, daily_chat_key)
        except Exception as daily_record_error:
            try:
                self.service.log("日级消息记录异常: %s" % daily_record_error)
            except Exception:
                pass

        if scene == "group":
            auto_repeat_result = self._auto_repeat(event, config)
            if auto_repeat_result is True:
                return
            if auto_repeat_result == "skip":
                self._remember_group_context(chat_key, event, "[复读接龙]")
                self._record_skip("repeat_chain_suppressed")
                return

        text = skills_ext.event_text(event)
        command_text = re.sub(r"@(?:你|\d+)\s*", "", text).strip()
        face_ids = [as_text(item) for item in (event.get("face_ids") or []) if as_text(item)]
        if face_ids and text == "".join("[表情:%s]" % item for item in face_ids) and not event.get("at_qqs"):
            self._record_skip("face_only")
            return
        if event.get("media_count"):
            try:
                voice_text = self.service.asr.transcribe_event(event, config)
            except Exception as voice_error:
                voice_text = ""
                self.service.log("语音转写异常: %s" % voice_error)
            if voice_text:
                text = (text + "\n[语音] " + voice_text).strip()
                command_text = re.sub(r"@(?:你|\d+)\s*", "", text).strip()
            elif skills_ext.extract_voice(event):
                text = (text + "\n[语音消息，没听清]").strip()
                command_text = re.sub(r"@(?:你|\d+)\s*", "", text).strip()
        if event.get("forward_ids"):
            try:
                forward_text = skills_ext.forward_context(self.service, event)
            except Exception as forward_error:
                forward_text = ""
                self.service.log("合并转发展开失败: %s" % forward_error)
            if forward_text:
                text = (text + "\n" + forward_text).strip()
                command_text = re.sub(r"@(?:你|\d+)\s*", "", text).strip()
        skills_cfg = config.get("skills") if is_object(config.get("skills")) else {}
        at_self = False
        mentioned = False
        quoted_id = ""
        reply_at_target = ""
        observe_key = "group:%s" % group_id if scene == "group" else "private:%s" % user_id
        observe_text = text or ("[图片]" if safe_int(event.get("media_count"), 0) > 0 else "")
        if observe_text and not command_text.startswith("/"):
            try:
                self.service.profiles.record_message(event, observe_text, scene, observe_key)
            except Exception as profile_error:
                self.service.log("人物印象记录失败: %s: %s" % (profile_error.__class__.__name__, profile_error))
            # Relationship/mood updates are model-driven in ReflectionEngine.
            # Keep only message/profile recording here so a word list cannot
            # silently transfer one person's insult onto an unrelated speaker.
        sleep_target = group_id if scene == "group" else user_id
        if not command_text.startswith("/") and not background_video:
            try:
                sleep_action = self.service.sleep.handle(event, scene, sleep_target, observe_key, user_id, observe_text or text)
                if sleep_action == "hold":
                    self._record_skip("sleep_hold")
                    return
                if sleep_action == "wake":
                    self.service.sleep.wake(observe_key, config)
                    return
            except Exception as sleep_error:
                self.service.log("睡眠处理失败: %s: %s" % (sleep_error.__class__.__name__, sleep_error))

        pricing_cfg = config.get("pricing") if is_object(config.get("pricing")) else {}
        try:
            peak_mode = bool(pricing_cfg.get("enabled", True)) and not pricing_off_peak(config)
        except Exception:
            peak_mode = False
        perms_global = config.get("permissions") if is_object(config.get("permissions")) else {}
        owner_ids = [str(item) for item in (perms_global.get("ownerUsers") or [])]
        owner_private = bool(scene == "private" and str(user_id) in owner_ids)

        if scene == "group" and not background_video:
            at_qqs = [as_text(item).strip() for item in (event.get("at_qqs") or [])]
            if self_id:
                at_self = any(qq == str(self_id) for qq in at_qqs)
            else:
                at_self = bool(at_qqs)
            # Never auto-@ an arbitrary third person merely because the incoming
            # message contains one @.  Auto-@ defaults to the sender; if the model
            # really wants to address the mentioned person it must explicitly use
            # [[at:QQ]] in the final content.
            prefix = as_text(auto.get("triggerPrefix")).strip()
            group_reply_mode = as_text(auto.get("groupReplyMode")).lower()
            with self.service.state_store.lock:
                group_modes = (self.service.state_store.data.get("groupReplyModes") or {})
            group_override = as_text(group_modes.get("group:%s" % group_id)).lower()
            if group_override in ("at", "all", "prob"):
                group_reply_mode = group_override
            group_reply_cfg = (skills_cfg.get("groupReply") if is_object(skills_cfg.get("groupReply")) else {})
            names = [as_text(self.service.state_store.snapshot().get("qq", {}).get("nickname"))]
            names.extend(as_text(item) for item in (group_reply_cfg.get("botNames") or []))
            mentioned = any(name and name in command_text for name in names if name)
            quoted_id = skills_ext.quoted_message_id(event)
            is_command = command_text.startswith("/")
            perms = config.get("permissions") if is_object(config.get("permissions")) else {}
            is_owner = str(user_id) in [str(item) for item in (perms.get("ownerUsers") or [])]
            is_media = (safe_int(event.get("media_count"), 0) > 0 or safe_int(event.get("share_count"), 0) > 0
                        or bool(event.get("forward_ids")))
            owner_share = bool(is_owner and event.get("share_count"))
            owner_media = bool(is_owner and is_media)
            explicit_addressed = at_self or mentioned or quoted_id or is_command
            emotional_addressed = False
            if group_reply_cfg.get("emotionalBypass", True):
                try:
                    emotional_addressed = skills_ext.is_emotional_message(text)
                except Exception:
                    emotional_addressed = False
            addressed = explicit_addressed or owner_share or owner_media or emotional_addressed
            was_addressed = explicit_addressed or emotional_addressed
            if not addressed:
                if group_reply_mode == "prob":
                    with self.service.state_store.lock:
                        chances = self.service.state_store.data.get("groupInterjectChance") or {}
                    try:
                        chance = int(chances.get("group:%s" % group_id, auto.get("interjectProbability", 30)))
                    except (TypeError, ValueError):
                        chance = 30
                    chance = clamp_int(chance, 0, 100, 30)
                    if is_media:
                        chance = clamp_int(auto.get("interjectMediaProbability"), 0, 100, 80)
                    if peak_mode:
                        try:
                            factor = float(pricing_cfg.get("peakReplyFactor", 0.6))
                        except (TypeError, ValueError):
                            factor = 0.6
                        chance = max(0, min(100, int(round(chance * factor))))
                    if random.random() * 100 >= chance:
                        marked = text or ("[发了一张图片]" if is_media else "")
                        self._remember_group_context("group:%s" % group_id, event, marked)
                        self._record_skip("group_interject_skip")
                        return
                elif group_reply_mode != "all":
                    self._remember_group_context("group:%s" % group_id, event, text)
                    self._record_skip("group_not_addressed")
                    return
            if prefix:
                if not command_text.startswith(prefix):
                    self._remember_group_context("group:%s" % group_id, event, text)
                    self._record_skip("group_prefix_mismatch")
                    return
                command_text = command_text[len(prefix):].strip()
                text = text[len(prefix):].strip()
        if not command_text:
            media_count = safe_int(event.get("media_count"), 0)
            if media_count > 0:
                text = "[发了一张图片/视频，没有文字]"
                command_text = text
            elif scene == "group" and at_self:
                text = "[只 @ 了你一下，没有文字]"
                command_text = text
            elif scene == "group" and (quoted_id or mentioned):
                text = "[只叫了你一下，没有正文]"
                command_text = text
            else:
                self._record_skip("empty_content")
                return

        chat_key = "group:%s" % group_id if scene == "group" else "private:%s" % user_id
        target = group_id if scene == "group" else user_id
        if not background_video and self._is_video_event(event) and not command_text.startswith("/"):
            try:
                ack = "我看看这个视频，稍等一下～" if scene == "private" else "我看看这个视频，稍等一下"
                ack_result = skills_ext.send_text(self.service, scene, target, ack)
                if isinstance(ack_result, dict) and ack_result.get("ok"):
                    self._record_daily_assistant(chat_key, ack, source="video_ack")
            except Exception as ack_error:
                self.service.log("视频处理提示发送失败: %s" % ack_error)
            clone = dict(event)
            clone["_video_background"] = True
            try:
                self.video_queue.put_nowait(clone)
                self.service.log("视频已进入后台队列: chat=%s msg=%s" % (chat_key, message_id))
            except queue.Full:
                self.service.log("视频后台队列已满")
            return
        skills_cfg = config.get("skills") if is_object(config.get("skills")) else {}
        images_cfg = skills_cfg.get("images") if is_object(skills_cfg.get("images")) else {}
        image_paths = []
        image_meme_text = ""
        storyboard_note = ""
        if (int(event.get("media_count") or 0) > 0 or skills_ext.quoted_message_id(event)) and images_cfg.get("enabled", True):
            image_paths, media_errors = skills_ext.prepare_media(self.service, event, chat_key, message_id, config)
            if media_errors:
                self.service.log("媒体处理失败: %s" % "；".join(media_errors[:3]))
        for media_path in image_paths:
            if as_text(media_path).endswith(".storyboard.png"):
                try:
                    with open(media_path + ".txt", "r", encoding="utf-8") as handle:
                        storyboard_note = handle.read()[:6000]
                except OSError:
                    storyboard_note = ""
                self._remember_video_context(chat_key, message_id, image_paths)
                break
        image_type_text = ""
        try:
            types = event.get("image_types") or []
            labels = []
            for item in types:
                if not isinstance(item, dict):
                    continue
                sub_type = safe_int(item.get("sub_type"), 0)
                if sub_type == -1:
                    labels.append("QQ商城表情（不要描述图中内容，只按情绪/梗接）")
                    continue
                summary = as_text(item.get("summary"))
                if sub_type == 0:
                    label = "普通图片/原图"
                elif sub_type == 1:
                    label = "动画表情/贴纸（不要描述图中内容，只按情绪/梗接）"
                elif sub_type == 7:
                    label = "QQ表情包（不要描述图中内容，只按情绪/梗接）"
                else:
                    label = "表情/图片（类型%s）" % sub_type
                labels.append(label + ((" " + summary) if summary else ""))
            if image_paths and skills_ext.quoted_message_id(event):
                labels.append("引用消息中的图片/表情")
            if labels:
                image_type_text = "[图片类型] 本消息收到：" + "；".join(labels) + "。自己判断，不要向用户解释类型/大小/分辨率。\n"
        except Exception:
            image_type_text = ""
        if image_paths:
            try:
                fingerprint = skills_ext.image_fingerprint(image_paths[0])
                if fingerprint:
                    image_meme_text = self.service.memories.image_meme_prompt(fingerprint)
                    self.service.memories.set_pending_image(chat_key, fingerprint, user_id)
                    if text and not text.startswith("[图片"):
                        self.service.memories.learn_pending_image(chat_key, text, user_id)
            except Exception as meme_error:
                self.service.log("图片梗查找失败: %s" % meme_error)
        else:
            try:
                self.service.memories.learn_pending_image(chat_key, text, user_id)
            except Exception as meme_error:
                self.service.log("图片梗学习失败: %s" % meme_error)
        if event.get("share_count"):
            try:
                share_text, share_images = skills_ext.enrich_share(self.service, event, chat_key, message_id, config)
                if share_text:
                    text = (text + "\n" + share_text).strip()
                if share_images:
                    image_paths.extend(share_images)
                    self._remember_video_context(chat_key, message_id, image_paths)
            except Exception as share_error:
                self.service.log("分享解析失败: %s: %s" % (share_error.__class__.__name__, share_error))
        if command_text in ("/reset", "/clear", "重置", "清空记忆"):
            if scene == "group":
                perms = config.get("permissions") if is_object(config.get("permissions")) else {}
                if str(user_id) not in [str(item) for item in (perms.get("ownerUsers") or [])]:
                    self.service.send_message_now(scene, target, "群聊记忆重置仅主人可用")
                    return
            self._clear_history(chat_key)
            skills_ext.cleanup_chat_images(self.service, chat_key)
            sent = self.service.send_message_now(scene, target, "对话记忆已重置。")
            if sent.get("ok"):
                self.service.state_store.incr("processed")
                self.service.state_store.incr("replied")
                self.service.log("指令 /reset 已处理 chat=%s" % chat_key)
            else:
                self._record_error("发送 /reset 回复失败: %s" % sent.get("error"))
            return
        if command_text in ("/help", "/帮助"):
            sent = self.service.send_message_now(scene, target, HELP_TEXT)
            if sent.get("ok"):
                self.service.state_store.incr("processed")
                self.service.state_store.incr("replied")
            else:
                self._record_error("发送 /help 回复失败: %s" % sent.get("error"))
            return
        if skills_ext.handle_command(self.service, event, scene, target, chat_key, command_text, config,
                                      image_paths=image_paths):
            return

        quote = skills_ext.quoted_context(self.service, event)
        if scene == "group" and quote and str(quote.get("user_id")) != str(self_id) and not at_self:
            group_reply_cfg = (skills_cfg.get("groupReply") if is_object(skills_cfg.get("groupReply")) else {})
            names = [as_text(self.service.state_store.snapshot().get("qq", {}).get("nickname"))]
            names.extend(as_text(item) for item in (group_reply_cfg.get("botNames") or []))
            mentioned = any(name and name in text for name in names if name)
            if not mentioned and not group_reply_cfg.get("quoteOthers", False):
                self._remember_group_context(chat_key, event, text)
                self._record_skip("group_quote_other")
                return
        sender_name = first_non_blank(event.get("sender_name"), user_id)
        group_context = ""
        if scene == "group":
            group_context = self._take_group_context(chat_key)
        forward_text = ""
        media_rows = []
        for item in (event.get("image_types") or []):
            if isinstance(item, dict):
                media_rows.append({"type": "image", "sub_type": safe_int(item.get("sub_type"), 0),
                                   "summary": as_text(item.get("summary"))})
        if event.get("forward_ids"):
            media_rows.append({"type": "forward", "expanded": as_text(forward_text)})
        if skills_ext.extract_voice(event):
            media_rows.append({"type": "voice", "transcript": as_text(text)})
        recent_speakers = [item for item in (event.get("_recent_speakers") or [])
                           if not (isinstance(item, dict) and self.is_recalled(item.get("message_id")))]
        envelope = context_ext.build_envelope(
            self.service, event, config, text, quote=quote, media=media_rows,
            addressed=bool(was_addressed), reply_target=reply_at_target,
            recent_speakers=recent_speakers,
            group_context=group_context, image_meme=image_meme_text,
            image_types=(image_type_text.replace("[图片类型] ", "").replace("。自己判断，不要向用户解释类型/大小/分辨率。", "").strip()))
        model_text = context_ext.render(envelope)
        history_text = model_text
        if storyboard_note:
            model_text = "[视频分镜（一张宫格图 + 每格时间/台词索引）]\n" + storyboard_note + "\n" + model_text
        try:
            dynamic_text = skills_ext.build_dynamic_prompt(self.service, config, chat_key, scene, user_id)
        except Exception:
            dynamic_text = ""
        if dynamic_text:
            model_text = dynamic_text + "\n" + model_text
        try:
            self.service.state_store.update(lambda data: data.setdefault("lastEnvelope", {}).__setitem__(chat_key, envelope))
        except Exception:
            pass
        try:
            meme_text = skills_ext.meme_context(self.service, text, chat_key=chat_key, config=config)
        except Exception:
            meme_text = ""
        if meme_text:
            model_text = "[梗知识（仅供参考）]\n" + meme_text + "\n" + model_text
        try:
            auto_meme = skills_ext.auto_meme_search(self.service, text, config)
        except Exception as meme_error:
            auto_meme = ""
            self.service.log("自动查梗失败: %s" % meme_error)
        if auto_meme:
            model_text = "[联网查梗]\n" + auto_meme[:1200] + "\n" + model_text
        cooldown_ms = safe_int(auto.get("cooldownMs"), 0)
        last_at = safe_int(self.last_processed_ms.get(chat_key), 0)
        now = now_ms()
        if not background_video and cooldown_ms > 0 and last_at > 0 and (now - last_at) < cooldown_ms:
            if scene == "group":
                self._remember_group_context(chat_key, event, text)
            self._record_skip("cooldown")
            return
        self.last_processed_ms[chat_key] = now

        max_history = safe_int(auto.get("maxHistory"), 12)
        meme_lookup_request = bool(re.search(r"梗库|梗百科", text))
        insist = skills_ext.wants_verification(text) and not meme_lookup_request
        effort = skills_ext.choose_effort(self.service, config, scene, chat_key, text, insist=insist)
        if pricing_cfg.get("ownerPrivateMax", True) and owner_private:
            effort = "max"
        elif peak_mode:
            effort = "low"
        image_budget = clamp_int(images_cfg.get("maxHistoryImages"), 0, 20, 6)
        system_prompt = skills_ext.build_system_prompt(self.service, config, chat_key, scene, user_id=user_id, include_dynamic=False)
        try:
            sticker_block = skills_ext.sticker_shortlist(config, text, has_image=bool(image_paths))
        except Exception as sticker_error:
            sticker_block = ""
            self.service.log("表情候选失败: %s" % sticker_error)
        if sticker_block:
            model_text = "【表情候选】\n" + sticker_block + "\n" + model_text
        if insist:
            model_text = ("【核实模式】用户坚持要你查证、质疑你编造或骂了你。这次必须先调用 web_search 核实事实（可以多搜几轮），"
                          "再按人设回应；如果之前说错了，嘴硬/找借口/认错都可以，但事实必须按搜索结果说。\n" + model_text)
        if peak_mode and not owner_private:
            model_text = ("【峰时省钱模式】现在是 DeepSeek 价格峰时：回复尽量短，少展开，能一句就一句；不要长篇大论，不要重复解释。\n" + model_text)
        daily_turn_id = safe_int(event.get("_conversation_turn_id"), 0)
        daily_messages = None
        if daily_turn_id:
            daily_messages = self._build_daily_messages(
                config, chat_key, scene, user_id, daily_turn_id, system_prompt)
        if daily_messages is not None:
            messages = daily_messages
        else:
            messages = [{"role": "system", "content": system_prompt}]
            for item in self._history_messages(chat_key, max_history):
                role = first_non_blank(item.get("role"))
                content, image_budget = skills_ext.history_content(item, image_budget)
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        current_content = skills_ext.build_user_content(model_text, image_paths)
        messages.append({"role": "user", "content": current_content})
        if daily_turn_id and getattr(self.service, "conversation", None) is not None:
            try:
                self.service.conversation.set_prompt(daily_turn_id, model_text, image_paths)
            except Exception as daily_prompt_error:
                self.service.log("记录日级 prompt 失败: %s" % daily_prompt_error)

        request_started_ms = now_ms()
        if scene == "group" and not was_addressed and not background_video:
            with self.service.state_store.lock:
                merge_override = self.service.state_store.data.get("groupMergeMsOverride")
            if merge_override is not None:
                merge_ms = clamp_int(merge_override, 0, 5000, 1200)
            else:
                merge_ms = clamp_int(auto.get("groupMergeMs"), 0, 5000, 1200)
            if merge_ms > 0:
                time.sleep(merge_ms / 1000.0)

        if self.is_recalled(message_id):
            if scene == "group":
                self._remember_group_context(chat_key, event, "[消息已撤回]")
            self._record_skip("recalled")
            return

        self.service.log("调用 DeepSeek: chat=%s 用户=%s 长度=%d 图片=%d effort=%s insist=%s quote=%s" % (
            chat_key, user_id, len(text), len(image_paths), effort, "1" if insist else "0",
            as_text(quote.get("nickname")) if quote else ""))
        tools_def = skills_ext.tool_definitions(config)
        if getattr(self.service, "current_video", None):
            tools_def.append(skills_ext.video_zoom_tool_definition(config))
        def tool_executor(name, arguments):
            if name == "video_zoom":
                return skills_ext.run_video_zoom(self.service, arguments or {}, config)
            if name == "search_meme_wiki":
                return skills_ext.search_meme_wiki(self.service, (arguments or {}).get("term") or (arguments or {}).get("query"))
            if name == "remember_memory":
                return skills_ext.remember_memory(self.service, config, scene, chat_key, user_id, arguments or {})
            if name == "propose_behavior_rule":
                return skills_ext.propose_behavior_rule(self.service, config, scene, chat_key, user_id, arguments or {})
            return skills_ext.run_tool(name, arguments or {}, config)
        result = call_deepseek(config, messages, tools=tools_def, tool_executor=tool_executor,
                               effort_override=effort)
        if result.get("toolTrace"):
            self.service.log("工具调用: %s" % " | ".join(result.get("toolTrace")[:4]))
        if insist and result.get("ok") and not result.get("toolRounds"):
            self.service.log("核实模式未触发搜索，追加提醒后重试一次: chat=%s" % chat_key)
            messages.append({"role": "assistant", "content": result.get("reply") or ""})
            messages.append({"role": "user", "content": "（系统提醒：用户要求核实，你刚才没有调用 web_search。现在必须调用 web_search 查证后再回答。）"})
            retry = call_deepseek(config, messages, tools=tools_def, tool_executor=tool_executor,
                                  effort_override="high")
            if retry.get("ok"):
                result = retry
        self.service.note_deepseek_result(result)
        try:
            usage = result.get("usage") or {}
            self.service.log("DeepSeek usage: prompt=%s completion=%s reasoning=%s cache_hit=%s cache_miss=%s" % (
                usage.get("prompt_tokens", usage.get("input_tokens", "?")),
                usage.get("completion_tokens", usage.get("output_tokens", "?")),
                usage.get("reasoning_tokens", "?"),
                usage.get("prompt_cache_hit_tokens", "?"),
                usage.get("prompt_cache_miss_tokens", "?")))
            self.service.record_cache_usage(chat_key, usage)
        except Exception:
            pass
        if not result.get("ok"):
            self._record_error("DeepSeek: %s" % result.get("error"))
            return
        reply = as_text(result.get("reply")).strip()
        if not reply:
            self._record_error("DeepSeek 返回空回复")
            return
        if self.is_recalled(message_id):
            self._record_skip("recalled_after_model")
            return
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[:MAX_REPLY_CHARS] + "..."
        if scene == "group" and not was_addressed:
            last_in = safe_int(self.last_incoming_ms.get(chat_key), 0)
            last_user = as_text(self.last_incoming_user.get(chat_key))
            if last_in > request_started_ms and last_user == user_id:
                self._append_history(chat_key, "user", history_text, max_history, images=image_paths, message_id=message_id)
                self._record_skip("group_multi_message_superseded")
                return
        sticker_started_at = int(time.time() * 1000)
        suppress_self_claim = any(isinstance(item, dict) and safe_int(item.get("sub_type"), 0) in (1, 7)
                                  for item in (event.get("image_types") or []))
        outcome = skills_ext.deliver_reply(self.service, event, scene, target, chat_key, reply, config,
                                           image_paths=image_paths, allow_auto_at=was_addressed,
                                           auto_at_target=reply_at_target, suppress_self_claim=suppress_self_claim)
        try:
            custom_sent = skills_ext.maybe_custom_sticker(self.service, scene, target, chat_key, config, text, reply, sticker_started_at)
        except Exception as custom_error:
            custom_sent = False
            self.service.log("自定义梗图异常: %s" % custom_error)
        # Sticker choice is model-driven; no bridge-level forced sticker.
        if not outcome.get("ok"):
            self._record_error("发送 QQ 消息失败: %s" % outcome.get("error"))
            return
        if outcome.get("silent"):
            self._append_history(chat_key, "user", history_text, max_history, images=image_paths, message_id=message_id)
            try:
                self.service.memories.consider(chat_key, scene, target, user_id, text)
            except Exception as memory_error:
                self.service.log("记忆抽取排队失败: %s" % memory_error)
            self.service.state_store.incr("processed")
            self.service.log("AI 选择沉默: chat=%s" % chat_key)
            return

        self._append_history(chat_key, "user", history_text, max_history, images=image_paths, message_id=message_id)
        self._append_history(chat_key, "assistant", reply, max_history)
        self._record_daily_assistant(chat_key, reply, source="auto")
        try:
            self.service.memories.consider(chat_key, scene, target, user_id, text)
        except Exception as memory_error:
            self.service.log("记忆抽取排队失败: %s" % memory_error)
        try:
            self.service.reflections.consider(envelope, reply, chat_key, scene, target, user_id)
        except Exception as reflection_error:
            self.service.log("反思排队失败: %s" % reflection_error)

        def mutate(data):
            data["lastReplyAt"] = now_ms()
            auto_state = data.setdefault("autoReply", {})
            auto_state["lastProcessedAt"] = now_ms()
            auto_state["lastReplyAt"] = now_ms()
            auto_state["lastError"] = ""
        self.service.state_store.update(mutate)
        self.service.state_store.incr("processed")
        self.service.state_store.incr("replied")
        self.service.log("自动回复成功: chat=%s 回复长度=%d 消息数=%s 耗时=%sms" % (
            chat_key, len(reply), outcome.get("sent"), result.get("elapsedMs")))


# ---------------------------------------------------------------------------
# udocker 生命周期管理
# ---------------------------------------------------------------------------

def expand_template(template, values):
    """把 'run {name}' 形式的命令模板替换占位符并拆分为 argv。"""
    text = as_text(template).strip()
    if not text:
        return []
    for key, value in values.items():
        text = text.replace("{%s}" % key, as_text(value))
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


class UDockerManager:
    """通过 subprocess 管理 udocker 容器；命令模板/超时可用 <state-dir>/udocker_commands.json 覆盖。"""

    COMMON_BINARIES = [
        "/usr/local/bin/udocker",
        "/usr/bin/udocker",
        "/bin/udocker",
        "/root/.local/bin/udocker",
        "/opt/udocker/udocker",
    ]

    def __init__(self, service, binary_override=""):
        self.service = service
        self.lock = threading.RLock()
        self.overrides_path = os.path.join(service.state_dir, "udocker_commands.json")
        self._overrides = {}
        self._overrides_mtime_ns = -1
        self._binary_override = as_text(binary_override).strip()
        self.status_cache = {"installed": False, "version": "", "path": "", "error": "", "checkedAt": 0}
        self._status_checked_at = 0
        self.container_cache = {}
        self._container_checked_at = 0
        self._container_log_default = DEFAULT_CONTAINER_LOG
        self.reload_overrides(force=True)
        self._ensure_default_overrides_file()

    # ---- 配置 ----

    def reload_overrides(self, force=False):
        with self.lock:
            try:
                mtime_ns = os.stat(self.overrides_path).st_mtime_ns
            except OSError:
                mtime_ns = -1
            if not force and mtime_ns == self._overrides_mtime_ns:
                return
            self._overrides_mtime_ns = mtime_ns
            raw = read_json_file(self.overrides_path) if mtime_ns >= 0 else {}
            self._overrides = raw if is_object(raw) else {}

    def _commands(self):
        with self.lock:
            overrides = self._overrides.get("commands")
        merged = dict(DEFAULT_UDOCKER_COMMANDS)
        if is_object(overrides):
            for key, value in overrides.items():
                merged[key] = as_text(value)
        return merged

    def _timeouts(self):
        with self.lock:
            overrides = self._overrides.get("timeouts")
        merged = dict(DEFAULT_UDOCKER_TIMEOUTS)
        if is_object(overrides):
            for key, value in overrides.items():
                if key in merged:
                    merged[key] = clamp_int(value, 1000, 7200000, merged[key])
        return merged

    def _dirs(self):
        with self.lock:
            configured = self._overrides.get("dirs")
        dirs = dict(DEFAULT_UDOCKER_DIRS)
        if is_object(configured):
            for key, value in configured.items():
                if key in dirs:
                    dirs[key] = first_non_blank(value, dirs[key])
        return dirs

    def _execmode(self):
        with self.lock:
            configured = first_non_blank(self._overrides.get("execmode"))
        return configured or DEFAULT_EXECMODE

    def _container_log_path(self):
        with self.lock:
            configured = first_non_blank(self._overrides.get("containerLog"))
        return configured or self._container_log_default

    def _platform(self):
        with self.lock:
            configured = first_non_blank(self._overrides.get("platform"))
        return configured or DEFAULT_PLATFORM

    def _values(self, config):
        dirs = self._dirs()
        return {
            "name": first_non_blank(config.get("containerName"), "snowluma"),
            "image": first_non_blank(config.get("image"), ""),
            "webuiPort": clamp_int(config.get("webuiPort"), 1, 65535, 5099),
            "webuiHost": first_non_blank(config.get("webuiHost"), "127.0.0.1"),
            "onebotHost": first_non_blank(config.get("onebotHost"), "127.0.0.1"),
            "onebotPort": clamp_int(config.get("onebotPort"), 1, 65535, 3001),
            "execmode": self._execmode(),
            "platform": self._platform(),
            "dataDir": dirs["data"],
            "configDir": dirs["config"],
            "localShareDir": dirs["localShare"],
            "homeDir": dirs["home"],
            "udockerDir": dirs["udocker"],
        }

    def _ensure_dirs(self, values):
        for key in ("dataDir", "configDir", "localShareDir"):
            path = first_non_blank(values.get(key))
            if not path:
                continue
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as error:
                self.service.log("创建容器数据目录失败 %s: %s" % (path, error))

    def _default_overrides_payload(self):
        return {
            "version": 1,
            "_readme": ("SnowLuma 桥接服务的 udocker 命令模板覆盖（可选，服务启动时若不存在会自动生成）。"
                        "修改后无需重启：执行容器动作时自动按 mtime 重载。"
                        "占位符：{image} {name} {webuiPort} {onebotPort} {execmode} {dataDir} {configDir} "
                        "{localShareDir} {homeDir} {udockerDir}。baseCommand 可用 "
                        "SNOWLUMA_UDOCKER_CMD 环境变量覆盖（如 \"udocker --allow-root\"）。"),
            "baseCommand": ["udocker", "--allow-root"],
            "execmode": DEFAULT_EXECMODE,
            "platform": DEFAULT_PLATFORM,
            "dirs": dict(DEFAULT_UDOCKER_DIRS),
            "commands": dict(DEFAULT_UDOCKER_COMMANDS),
            "timeouts": dict(DEFAULT_UDOCKER_TIMEOUTS),
            "containerLog": DEFAULT_CONTAINER_LOG,
        }

    def _ensure_default_overrides_file(self):
        if os.path.exists(self.overrides_path):
            return
        try:
            atomic_write_json(self.overrides_path, self._default_overrides_payload(), 0o600)
            self.reload_overrides(force=True)
            self.service.log("已生成默认 udocker_commands.json: %s" % self.overrides_path)
        except OSError as error:
            self.service.log("生成默认 udocker_commands.json 失败: %s" % error)

    # ---- 可执行文件探测 ----

    def resolve_binary(self):
        if self._binary_override:
            return self._binary_override
        with self.lock:
            configured = first_non_blank(self._overrides.get("binary"))
        if configured:
            return configured
        env_binary = first_non_blank(os.environ.get("SNOWLUMA_UDOCKER_BIN"), os.environ.get("UDOCKER_BIN"))
        if env_binary:
            return env_binary
        search_path = os.environ.get("PATH", "") + ":/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/opt/udocker"
        found = shutil.which("udocker", path=search_path)
        if found:
            return found
        for candidate in self.COMMON_BINARIES:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return ""

    def _global_args(self):
        with self.lock:
            explicit = self._overrides.get("globalArgs")
            allow_root = self._overrides.get("allowRoot")
        if isinstance(explicit, list):
            return [as_text(item) for item in explicit if as_text(item)]
        if allow_root is False:
            return []
        # Operit PRoot 内 euid=0，udocker 需要 --allow-root 才肯执行（可在 udocker_commands.json 关闭）
        try:
            if os.geteuid() == 0 and allow_root is not False:
                return ["--allow-root"]
        except AttributeError:
            pass
        return []

    def _base_command(self):
        """udocker 基础命令：优先级 env SNOWLUMA_UDOCKER_CMD > udocker_commands.json.baseCommand > 自动探测。"""
        env_value = first_non_blank(os.environ.get("SNOWLUMA_UDOCKER_CMD"))
        if env_value:
            try:
                parsed = shlex.split(env_value)
            except ValueError:
                parsed = env_value.split()
            if parsed:
                return parsed
        with self.lock:
            configured = self._overrides.get("baseCommand")
        if isinstance(configured, list):
            items = [as_text(item) for item in configured if as_text(item)]
            if items:
                return items
        elif configured is not None:
            try:
                items = shlex.split(as_text(configured))
            except ValueError:
                items = as_text(configured).split()
            if items:
                return items
        binary = self.resolve_binary()
        if not binary:
            return []
        return [binary] + self._global_args()

    def _base(self):
        base = self._base_command()
        if not base:
            return []
        binary = self.resolve_binary()
        if binary and base[0] == "udocker":
            base = [binary] + base[1:]
        return base

    def _env(self):
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        env["PYTHONUNBUFFERED"] = "1"
        return env

    # ---- 命令执行 ----

    def _command_text(self, argv):
        parts = self._base_command() + [as_text(item) for item in argv]
        if not parts:
            parts = ["udocker"] + [as_text(item) for item in argv]
        return " ".join(shlex.quote(item) for item in parts)

    def _run(self, argv, timeout_ms, label):
        started = now_ms()
        result = {
            "ok": False,
            "label": label,
            "command": self._command_text(argv),
            "exitCode": None,
            "timedOut": False,
            "durationMs": 0,
            "stdout": "",
            "stderr": "",
            "output": "",
            "error": "",
        }
        base = self._base()
        if not base:
            result["error"] = "未找到 udocker 可执行文件"
            result["durationMs"] = now_ms() - started
            return result
        try:
            process = subprocess.Popen(
                base + [as_text(item) for item in argv],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                env=self._env(),
                start_new_session=True,
            )
        except FileNotFoundError:
            result["error"] = "未找到 udocker 可执行文件"
            result["durationMs"] = now_ms() - started
            return result
        except OSError as error:
            result["error"] = "%s: %s" % (error.__class__.__name__, error)
            result["durationMs"] = now_ms() - started
            return result
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=max(1.0, timeout_ms / 1000.0))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        output = ((stdout or "") + ("\n" if (stderr and stdout) else "") + (stderr or "")).strip()
        result["stdout"] = (stdout or "")[-8000:]
        result["stderr"] = (stderr or "")[-8000:]
        result["output"] = output[-12000:]
        result["exitCode"] = process.returncode
        result["timedOut"] = timed_out
        result["durationMs"] = now_ms() - started
        if timed_out:
            result["error"] = "命令执行超时（%dms）" % timeout_ms
        elif process.returncode != 0:
            result["error"] = "命令退出码 %s" % process.returncode
        else:
            result["ok"] = True
        return result

    def _spawn(self, argv, log_path):
        started = now_ms()
        result = {"ok": False, "pid": 0, "command": self._command_text(argv), "logFile": log_path, "error": ""}
        base = self._base()
        if not base:
            result["error"] = "未找到 udocker 可执行文件"
            return result
        out_handle = None
        try:
            ensure_dir(os.path.dirname(os.path.abspath(log_path)) or ".")
            out_handle = open(log_path, "ab")
        except OSError:
            out_handle = None
        try:
            process = subprocess.Popen(
                base + [as_text(item) for item in argv],
                stdout=out_handle if out_handle is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=self._env(),
                start_new_session=True,
            )
        except FileNotFoundError:
            result["error"] = "未找到 udocker 可执行文件"
            return result
        except OSError as error:
            result["error"] = "%s: %s" % (error.__class__.__name__, error)
            return result
        finally:
            if out_handle is not None:
                try:
                    out_handle.close()
                except OSError:
                    pass
        result["ok"] = True
        result["pid"] = process.pid
        result["durationMs"] = now_ms() - started
        return result

    # ---- 状态探测 ----

    def get_status(self, force=False):
        now = now_ms()
        with self.lock:
            if not force and self._status_checked_at and (now - self._status_checked_at) < 30000:
                return deep_copy(self.status_cache)
            self._refresh_status_locked()
            return deep_copy(self.status_cache)

    def _refresh_status_locked(self):
        binary = self.resolve_binary()
        if not binary:
            self.status_cache = {
                "installed": False,
                "version": "",
                "path": "",
                "error": "未找到 udocker（已检查 PATH 与常见路径；可用 udocker_commands.json 的 binary 指定）",
                "checkedAt": now_ms(),
            }
            self._status_checked_at = now_ms()
            return
        commands = self._commands()
        timeouts = self._timeouts()
        run = self._run(expand_template(commands.get("version", "version"), {}), timeouts["versionMs"], "udocker version")
        version = ""
        for line in as_text(run.get("output")).splitlines():
            if first_non_blank(line):
                version = first_non_blank(line)[:200]
                break
        self.status_cache = {
            "installed": True,
            "version": version,
            "path": binary,
            "error": "" if run.get("ok") else first_non_blank(run.get("error"), run.get("stderr"), "udocker version 失败"),
            "checkedAt": now_ms(),
        }
        self._status_checked_at = now_ms()

    def _output_has_name(self, output, name):
        name = first_non_blank(name)
        if not name:
            return False
        for line in as_text(output).splitlines():
            tokens = re.split(r"\s+", line.strip())
            if name in tokens:
                return True
        return False

    def container_status(self, force=False):
        now = now_ms()
        config = self.service.config_store.get()
        values = self._values(config)
        name = values["name"]
        with self.lock:
            if (not force) and self._container_checked_at and (now - self._container_checked_at) < 15000:
                return deep_copy(self.container_cache)
        state = self.service.state_store.snapshot()
        stored_pid = safe_int(state.get("container", {}).get("pid"), 0)
        result = {
            "name": name,
            "image": values["image"],
            "exists": False,
            "running": False,
            "status": "unknown",
            "pid": stored_pid if pid_alive(stored_pid) else 0,
            "startedAt": safe_int(state.get("container", {}).get("startedAt"), 0),
            "execmode": self._execmode(),
            "logFile": self._container_log_path(),
            "baseCommand": " ".join(self._base_command()),
            "webuiPort": values["webuiPort"],
            "onebotPort": values["onebotPort"],
            "webuiUrl": "http://%s:%d" % (values["webuiHost"], values["webuiPort"]),
            "onebotWsUrl": "ws://%s:%d/" % (values["onebotHost"], values["onebotPort"]),
            "error": "",
            "checkedAt": now_ms(),
        }
        udocker_status = self.get_status(force=False)
        if not udocker_status.get("installed"):
            result["status"] = "udocker_missing"
            result["error"] = first_non_blank(udocker_status.get("error"))
            with self.lock:
                self.container_cache = result
                self._container_checked_at = now_ms()
            return deep_copy(result)
        commands = self._commands()
        timeouts = self._timeouts()
        ps_all = self._run(expand_template(commands.get("psAll", "ps -m"), values), timeouts["psMs"], "udocker psAll")
        ps_run = self._run(expand_template(commands.get("ps", "ps"), values), timeouts["psMs"], "udocker ps")
        exists = self._output_has_name(ps_all.get("output"), name)
        running = self._output_has_name(ps_run.get("output"), name)
        if stored_pid and pid_alive(stored_pid) and "udocker" in process_cmdline(stored_pid):
            running = True
        errors = []
        if not ps_all.get("ok"):
            errors.append("psAll: %s" % first_non_blank(ps_all.get("error"), "failed"))
        if not ps_run.get("ok"):
            errors.append("ps: %s" % first_non_blank(ps_run.get("error"), "failed"))
        if running:
            status_text = "running"
        elif exists:
            status_text = "created"
        else:
            status_text = "not_created"
        result.update({
            "exists": bool(exists),
            "running": bool(running),
            "status": status_text,
            "error": " | ".join(errors)[:500],
        })
        with self.lock:
            self.container_cache = result
            self._container_checked_at = now_ms()
        return deep_copy(result)

    def refresh(self):
        self.get_status(force=True)
        self.container_status(force=True)

    # ---- 容器动作 ----

    def _summary(self, run):
        return {
            "ok": bool(run.get("ok")),
            "command": run.get("command", ""),
            "exitCode": run.get("exitCode"),
            "timedOut": bool(run.get("timedOut")),
            "durationMs": run.get("durationMs", 0),
            "stdoutTail": as_text(run.get("stdout"))[-2000:],
            "stderrTail": as_text(run.get("stderr"))[-2000:],
            "error": first_non_blank(run.get("error")),
        }

    def _wait_for_running(self, expected, timeout_ms):
        deadline = time.time() + max(1.0, timeout_ms / 1000.0)
        last = self.container_status(force=True)
        while time.time() < deadline:
            last = self.container_status(force=True)
            if bool(last.get("running")) == bool(expected):
                return last
            time.sleep(1.0)
        return last

    def _start(self, config):
        values = self._values(config)
        self._ensure_dirs(values)
        current = self.container_status(force=True)
        if current.get("running"):
            return {"ok": True, "alreadyRunning": True, "message": "容器已在运行", "container": current}
        if not current.get("exists"):
            return {
                "ok": False,
                "error": "container_not_created",
                "message": "容器不存在：请先执行 pull + create；若容器名不同请在 config.json 的 containerName 中修改",
                "container": current,
            }
        commands = self._commands()
        timeouts = self._timeouts()
        argv = expand_template(commands.get("start", "run {name}"), values)
        container_log = self._container_log_path()
        spawned = self._spawn(argv, container_log)
        if not spawned.get("ok"):
            return {"ok": False, "error": spawned.get("error"), "command": spawned.get("command", ""),
                    "logFile": container_log}
        self.service.record_container_pid(spawned.get("pid", 0))
        container = self._wait_for_running(True, START_WAIT_MS)
        result = {
            "ok": bool(container.get("running")),
            "pid": spawned.get("pid", 0),
            "command": spawned.get("command", ""),
            "container": container,
            "logFile": container_log,
            "execmode": self._execmode(),
        }
        if not container.get("running"):
            log_tail = tail_text_file(container_log, 120)
            result["containerLogTail"] = log_tail[-3000:]
            if re.search(r"(start\.sh|entrypoint|exec format|not found|No such file)", log_tail, re.I):
                result["error"] = "start_script_not_found"
                result["hint"] = ("P2 execmode 下容器内 start.sh 报 not found（多层 proot 限制）。"
                                  "请参考 docs/01_udocker与SnowLuma部署命令.md 的路线 B/C；"
                                  "或在 udocker_commands.json 调整 execmode / start 模板后重试。")
                result["ok"] = False
            else:
                result["warning"] = "udocker 进程已启动，但 udocker ps 未显示运行状态；请查看 logs / container.log"
        return result

    def _find_proot_pids(self):
        """兜底：清理 udocker 容器残留的内层 proot 进程（docs/01 §6.4）。"""
        pids = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            entries = []
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == os.getpid():
                continue
            cmdline = process_cmdline(pid)
            if not cmdline:
                continue
            if "proot" in cmdline and "udocker/containers" in cmdline:
                pids.append(pid)
        return pids

    def _find_udocker_pids(self):
        config = self.service.config_store.get()
        name = first_non_blank(config.get("containerName"))
        binary = os.path.basename(self.resolve_binary() or "udocker")
        pids = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            entries = []
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == os.getpid():
                continue
            cmdline = process_cmdline(pid)
            if not cmdline:
                continue
            if binary in cmdline and name and name in cmdline:
                pids.append(pid)
        return pids

    def _stop(self):
        commands = self._commands()
        timeouts = self._timeouts()
        values = self._values(self.service.config_store.get())
        stop_template = first_non_blank(commands.get("stop"))
        stop_run = None
        if stop_template:
            stop_run = self._run(expand_template(stop_template, values), timeouts["defaultMs"], "udocker stop")
            if stop_run.get("ok"):
                container = self._wait_for_running(False, STOP_WAIT_MS)
                if not container.get("running"):
                    self.service.clear_container_pid()
                    return {
                        "ok": True,
                        "stopped": True,
                        "method": "stop_command",
                        "command": stop_run.get("command"),
                        "output": as_text(stop_run.get("output"))[-2000:],
                        "container": container,
                    }
        state = self.service.state_store.snapshot()
        stored_pid = safe_int(state.get("container", {}).get("pid"), 0)
        pids = []
        if stored_pid and pid_alive(stored_pid) and "udocker" in process_cmdline(stored_pid):
            pids.append(stored_pid)
        if not pids:
            pids = self._find_udocker_pids()
        if not pids:
            pids = self._find_proot_pids()
        if not pids and stop_run is not None and stop_run.get("ok"):
            container = self.container_status(force=True)
            if not container.get("running"):
                self.service.clear_container_pid()
                return {"ok": True, "stopped": True, "method": "stop_command",
                        "command": stop_run.get("command"),
                        "output": as_text(stop_run.get("output"))[-2000:], "container": container}
        if not pids:
            self.service.clear_container_pid()
            result = {"ok": True, "stopped": True, "alreadyStopped": True, "method": "none",
                      "detail": "未找到运行中的 udocker 进程"}
            if stop_run is not None:
                result["stopCommand"] = self._summary(stop_run)
            result["container"] = self.container_status(force=True)
            return result
        for pid in pids:
            kill_pid(pid, signal.SIGTERM, group=True)
        deadline = time.time() + max(1.0, STOP_WAIT_MS / 1000.0)
        while time.time() < deadline and any(pid_alive(pid) for pid in pids):
            time.sleep(0.5)
        remaining = [pid for pid in pids if pid_alive(pid)]
        for pid in remaining:
            kill_pid(pid, signal.SIGKILL, group=True)
        time.sleep(0.5)
        remaining = [pid for pid in remaining if pid_alive(pid)]
        self.service.clear_container_pid()
        container = self.container_status(force=True)
        result = {
            "ok": not remaining,
            "stopped": not remaining,
            "method": "process_kill",
            "pids": pids,
            "remainingPids": remaining,
            "container": container,
        }
        if stop_run is not None:
            result["stopCommand"] = self._summary(stop_run)
        return result

    def _container_log_tail(self, lines):
        container_log = self._container_log_path()
        text = tail_text_file(container_log, lines)
        source = "container.log"
        if not text:
            text = tail_text_file(self.service.log_file, lines)
            source = "bridge.log"
        return source, text

    def _logs(self, lines):
        lines = clamp_int(lines, 1, MAX_LOG_TAIL_LINES, 200)
        commands = self._commands()
        timeouts = self._timeouts()
        logs_template = first_non_blank(commands.get("logs"))
        if logs_template:
            run = self._run(expand_template(logs_template, self._values(self.service.config_store.get())), timeouts["defaultMs"], "udocker logs")
            text = as_text(run.get("output"))
            return {
                "ok": bool(run.get("ok")) or bool(text),
                "source": "command",
                "command": run.get("command"),
                "text": text[-20000:],
                "error": first_non_blank(run.get("error")),
            }
        log_file = self._container_log_path()
        source, text = self._container_log_tail(lines)
        if source == "bridge.log":
            log_file = self.service.log_file
        return {"ok": True, "source": source, "text": text, "lines": lines, "logFile": log_file}

    def run_action(self, action, lines=200):
        action = first_non_blank(action).lower()
        config = self.service.config_store.get()
        status = self.get_status(force=False)
        if action == "status":
            return {
                "ok": True,
                "result": {
                    "action": "status",
                    "udocker": self.get_status(force=False),
                    "container": self.container_status(force=False),
                },
                "log": "",
            }
        if action not in ("pull", "create", "setup", "start", "stop", "restart", "logs"):
            return {"ok": False, "error": "unsupported_action", "result": {"action": action}, "log": ""}
        if not status.get("installed"):
            message = first_non_blank(status.get("error"), "udocker 未安装")
            self.service.log("容器动作 %s 失败：%s" % (action, message))
            return {"ok": False, "error": message, "result": {"action": action, "installed": False}, "log": message}
        self.reload_overrides()
        config = self.service.config_store.get()
        values = self._values(config)
        commands = self._commands()
        timeouts = self._timeouts()
        started = now_ms()
        if action == "pull":
            run = self._run(expand_template(commands.get("pull", "pull {image}"), values), timeouts["pullMs"], "udocker pull")
            result = self._summary(run)
        elif action == "create":
            run = self._run(expand_template(commands.get("create", "create --name={name} {image}"), values), timeouts["defaultMs"], "udocker create")
            result = self._summary(run)
            if result.get("ok"):
                # udocker 没有 docker start：create 之后必须 setup --execmode=P2（docs/01 实测）
                setup_run = self._run(
                    expand_template(commands.get("setup", "setup --execmode={execmode} {name}"), values),
                    timeouts.get("setupMs", timeouts["defaultMs"]),
                    "udocker setup",
                )
                result["setup"] = self._summary(setup_run)
                result["execmode"] = values.get("execmode")
                if not setup_run.get("ok"):
                    result["ok"] = False
                    result["error"] = "setup_failed: %s" % first_non_blank(setup_run.get("error"), "udocker setup 失败")
                    result["hint"] = ("若 setup 报 execmode 相关错误，可在 udocker_commands.json 调整 execmode"
                                      "（默认 P2）或参考 docs/01_udocker与SnowLuma部署命令.md 的路线 B/C")
                else:
                    self.service.mark_container_created()
        elif action == "setup":
            run = self._run(
                expand_template(commands.get("setup", "setup --execmode={execmode} {name}"), values),
                timeouts.get("setupMs", timeouts["defaultMs"]),
                "udocker setup",
            )
            result = self._summary(run)
            result["execmode"] = values.get("execmode")
            if result.get("ok"):
                self.service.mark_container_created()
        elif action == "start":
            result = self._start(config)
        elif action == "stop":
            result = self._stop()
        elif action == "restart":
            stop_result = self._stop()
            start_result = self._start(config)
            result = {"ok": bool(start_result.get("ok")), "stop": stop_result, "start": start_result}
        else:
            logs_result = self._logs(lines)
            result = logs_result
        result["action"] = action
        result["durationMs"] = now_ms() - started
        result["container"] = self.container_status(force=True)
        log_line = "容器动作 %s: ok=%s duration=%sms detail=%s" % (
            action,
            result.get("ok"),
            result.get("durationMs"),
            json.dumps({key: result.get(key) for key in ("error", "exitCode", "timedOut", "command", "warning") if result.get(key) not in (None, "")},
                       ensure_ascii=False)[:800],
        )
        self.service.record_container_action(action, result)
        self.service.log(log_line)
        return {"ok": bool(result.get("ok")), "result": result, "log": log_line}


# ---------------------------------------------------------------------------
# 桥接服务主体
# ---------------------------------------------------------------------------

class ProactiveManager:
    """隔一段时间主动找话题：按 state.proactiveChats 里的会话定时生成一条消息。"""

    def __init__(self, service):
        self.service = service
        self.thread = None
        self.stop_event = threading.Event()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="snowluma-proactive", daemon=True)
        self.thread.start()

    def _loop(self):
        time.sleep(25)
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as error:
                try:
                    self.service.log("主动消息异常: %s: %s" % (error.__class__.__name__, error))
                except Exception:
                    pass
            self.stop_event.wait(60)

    def _chats(self):
        store = self.service.state_store
        with store.lock:
            chats = store.data.get("proactiveChats") or {}
            return {key: dict(value) for key, value in chats.items() if isinstance(value, dict)}

    def _tick(self):
        config = self.service.config_store.get()
        proactive = config.get("proactive") if isinstance(config.get("proactive"), dict) else {}
        if not proactive.get("enabled", True):
            return
        chats = self._chats()
        if not chats:
            return
        now = time.time()
        try:
            from zoneinfo import ZoneInfo
            now_dt = datetime.now(ZoneInfo(first_non_blank(config.get("timeZone"), "Asia/Shanghai")))
        except Exception:
            now_dt = datetime.now()
        hour = now_dt.hour
        quiet_start = safe_int(proactive.get("quietStart"), 23)
        quiet_end = safe_int(proactive.get("quietEnd"), 7)
        if quiet_start != quiet_end:
            if quiet_start < quiet_end:
                is_quiet = quiet_start <= hour < quiet_end
            else:
                is_quiet = hour >= quiet_start or hour < quiet_end
            if is_quiet:
                return
        min_silence = safe_int(proactive.get("minSilenceMinutes"), 240) * 60
        max_per_day = safe_int(proactive.get("maxPerDay"), 2)
        min_interval = safe_int(proactive.get("minIntervalMinutes"), 180) * 60
        day_key = now_dt.strftime("%Y-%m-%d")
        try:
            if hasattr(self.service, "mood") and not self.service.mood.proactive_allowed():
                self.service.log("心情较低，本轮不主动发消息")
                return
        except Exception:
            pass
        for chat_key, item in chats.items():
            if not item.get("enabled", True):
                continue
            scene = as_text(item.get("scene"))
            target = as_text(item.get("target"))
            if scene not in ("private", "group") or not target:
                continue
            store = self.service.state_store
            try:
                if hasattr(self.service, "sleep") and self.service.sleep.has_pending(chat_key):
                    continue
                with store.lock:
                    cooldown_until = int((store.data.get("sleepCooldownUntil") or {}).get(chat_key) or 0)
                if int(now * 1000) < cooldown_until:
                    continue
            except Exception:
                pass
            last_activity = self._last_activity(chat_key)
            if not last_activity:
                continue
            if now - last_activity < min_silence:
                continue
            with store.lock:
                record = dict((store.data.get("proactive") or {}).get(chat_key) or {})
            last_sent = safe_int(record.get("lastSentAt"), 0) / 1000.0
            if last_sent and now - last_sent < min_interval:
                continue
            if as_text(record.get("day")) == day_key and safe_int(record.get("count"), 0) >= max_per_day:
                continue
            minutes_silent = int((now - last_activity) / 60)
            reply = self._generate(config, chat_key, scene, target, minutes_silent)
            if not reply:
                continue
            fake_event = {"user_id": "", "message_id": "", "raw": {}}

            # Reserve the proactive slot before sending.  If the send itself
            # fails, do not retry the same proactive message on the next tick.
            def reserve(data):
                bucket = data.setdefault("proactive", {})
                record2 = bucket.get(chat_key) if isinstance(bucket.get(chat_key), dict) else {}
                record2["lastSentAt"] = int(now * 1000)
                record2["day"] = day_key
                record2["count"] = (safe_int(record2.get("count"), 0) + 1) if as_text(record2.get("day")) == day_key else 1
                bucket[chat_key] = record2
            store.update(reserve)
            outcome = skills_ext.deliver_reply(self.service, fake_event, scene, target, chat_key, reply, config,
                                               allow_auto_at=False)
            if not outcome.get("ok") or outcome.get("silent") or not outcome.get("sent"):
                self.service.log("主动消息发送失败，本轮不重试: chat=%s" % chat_key)
                continue
            self._append_history(chat_key, reply)
            self.service.log("主动消息已发送: chat=%s 本地时间=%s 沉默=%d分钟 字数=%d" % (
                chat_key, now_dt.strftime("%H:%M"), minutes_silent, len(reply)))

    def _last_activity(self, chat_key):
        try:
            history = self.service.auto_engine._history_messages(chat_key, 1)
            if history:
                return safe_int(history[-1].get("at"), 0) / 1000.0
        except Exception:
            pass
        return 0

    def _append_history(self, chat_key, reply):
        try:
            self.service.auto_engine._append_history(chat_key, "assistant", reply, 30)
            self.service.auto_engine._record_daily_assistant(chat_key, reply, source="proactive")
        except Exception:
            pass

    def _generate(self, config, chat_key, scene, target, minutes_silent):
        try:
            engine = self.service.auto_engine
            history = engine._history_messages(chat_key, 8)
        except Exception:
            history = []
        system_prompt = skills_ext.build_system_prompt(self.service, config, chat_key, scene)
        try:
            from zoneinfo import ZoneInfo
            local_hhmm = datetime.now(ZoneInfo(first_non_blank(config.get("timeZone"), "Asia/Shanghai"))).strftime("%H:%M")
        except Exception:
            local_hhmm = time.strftime("%H:%M")
        if scene == "group":
            instruction = ("（系统提示：现在是 %s，这个群已经 %d 分钟没人说话了。先判断现在是否适合主动开口；"
                           "不想说、没话题、或者大家可能在忙，就只输出 [[silent]]。想说的话最多 2 条，每条 1-10 字，"
                           "像群友随手发的一样；不要 @ 人、不要用需要对象的 token、不要提系统提示、不要解释。）") % (local_hhmm, minutes_silent)
        else:
            instruction = ("（系统提示：现在是 %s，你和对方已经 %d 分钟没说话了。先判断现在是否适合主动开口；"
                           "不想说就只输出 [[silent]]。想说的话最多 2 条，每条 1-10 字，像真人随手发的一样；"
                           "可以分享小事、吐槽、问一句；不要提系统提示，不要解释。）") % (local_hhmm, minutes_silent)
        messages = [{"role": "system", "content": system_prompt}]
        for item in history:
            messages.append({"role": as_text(item.get("role")), "content": as_text(item.get("content"))})
        messages.append({"role": "user", "content": instruction})
        result = call_deepseek(config, messages, effort_override="low")
        if not result.get("ok"):
            self.service.log("主动消息生成失败: %s" % result.get("error"))
            return ""
        return as_text(result.get("reply")).strip()


class ProfileManager:
    """人物印象库：按 QQ 记录最近发言、手动备注，并定期用 DeepSeek 生成长期印象。"""

    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.thread = None
        self.stop_event = threading.Event()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="snowluma-profiles", daemon=True)
        self.thread.start()

    def record_message(self, event, text, scene, chat_key):
        try:
            user_id = as_text(event.get("user_id")).strip()
            if not user_id:
                return
            config = self.service.config_store.get()
            profile_cfg = config.get("profiles") if is_object(config.get("profiles")) else {}
            if not profile_cfg.get("enabled", True):
                return
            nickname = as_text(event.get("sender_name")) or user_id
            now = int(time.time() * 1000)
            store = self.service.state_store
            should_summarize = False
            with store.lock:
                profiles = store.data.setdefault("userProfiles", {})
                item = profiles.get(user_id) if isinstance(profiles.get(user_id), dict) else {}
                item["nickname"] = nickname
                item["messages"] = safe_int(item.get("messages"), 0) + 1
                item["lastSeen"] = now
                recent = item.get("recent") if isinstance(item.get("recent"), list) else []
                recent.append({"text": as_text(text)[:200], "at": now, "scene": scene})
                item["recent"] = recent[-12:]
                item.setdefault("notes", [])
                item.setdefault("summary", "")
                item["summaryCount"] = safe_int(item.get("summaryCount"), 0)
                item["summaryAt"] = safe_int(item.get("summaryAt"), 0)
                profiles[user_id] = item
                if scene == "group" and chat_key:
                    participants = store.data.setdefault("groupParticipants", {})
                    bucket = participants.get(chat_key) if isinstance(participants.get(chat_key), dict) else {}
                    bucket[user_id] = nickname
                    if len(bucket) > 30:
                        for old_key in list(bucket.keys())[:len(bucket) - 30]:
                            bucket.pop(old_key, None)
                    participants[chat_key] = bucket
                store.dirty = True
                should_summarize = (
                    item["messages"] - item["summaryCount"] >= safe_int(profile_cfg.get("summarizeEvery"), 15)
                    and now - item["summaryAt"] >= safe_int(profile_cfg.get("minIntervalMinutes"), 30) * 60 * 1000
                    and len(item["recent"]) >= 5
                )
            if should_summarize:
                self.queue.put(user_id)
        except Exception as error:
            try:
                self.service.log("记录人物印象失败: %s: %s" % (error.__class__.__name__, error))
            except Exception:
                pass

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                user_id = self.queue.get(timeout=2)
            except queue.Empty:
                continue
            try:
                self._summarize(user_id)
            except Exception as error:
                try:
                    self.service.log("生成人物印象失败: %s: %s" % (error.__class__.__name__, error))
                except Exception:
                    pass

    def _summarize(self, user_id):
        config = self.service.config_store.get()
        store = self.service.state_store
        with store.lock:
            item = (store.data.get("userProfiles") or {}).get(user_id)
            if not isinstance(item, dict):
                return
            nickname = as_text(item.get("nickname")) or user_id
            recent = list(item.get("recent") or [])
            notes = list(item.get("notes") or [])
            old_summary = as_text(item.get("summary"))
        if len(recent) < 5:
            return
        samples = " || ".join(as_text(row.get("text"))[:80] for row in recent[-12:] if isinstance(row, dict))
        note_text = " || ".join(as_text(note)[:80] for note in notes[-8:])
        system_prompt = (
            "你是记忆整理助手。根据聊天片段，为这个人写 1-3 条简短、稳定、可长期保存的印象"
            "（称呼、性格、互动方式、长期兴趣、关系）。不要编造，不确定就不写；每行一条，最多 3 行，每行不超过 40 字。"
            "不要把一次性事件、某张图片或视频的具体内容、某次抽卡或游戏结果、某个临时话题写成人物特征；"
            "这些只能算近期事件，不写入长期印象。"
            "更新已有印象时，删掉或泛化已经过时的具体事件，不要原样继承。"
        )
        user_content = "昵称：" + nickname + "；QQ：" + user_id + "；已有印象：" + (old_summary or "无") + "；手动备注：" + (note_text or "无") + "；最近发言片段：" + samples
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
        result = call_deepseek(config, messages, effort_override="low")
        if not result.get("ok"):
            return
        summary = as_text(result.get("reply")).strip().replace("```", "").strip()
        if not summary:
            return
        summary = summary[:600]
        with store.lock:
            item = (store.data.get("userProfiles") or {}).get(user_id)
            if not isinstance(item, dict):
                return
            item["summary"] = summary
            item["summaryAt"] = int(time.time() * 1000)
            item["summaryCount"] = safe_int(item.get("messages"), 0)
            store.dirty = True
        self.service.log("人物印象已更新: qq=%s 字数=%d" % (user_id, len(summary)))

    def prompt_for(self, scene, chat_key, user_id):
        config = self.service.config_store.get()
        profile_cfg = config.get("profiles") if is_object(config.get("profiles")) else {}
        if not profile_cfg.get("enabled", True):
            return ""
        max_users = safe_int(profile_cfg.get("maxParticipants"), 8)
        store = self.service.state_store
        order = []
        with store.lock:
            profiles = store.data.get("userProfiles") or {}
            participants = (store.data.get("groupParticipants") or {}).get(chat_key) or {}
            seen = set()
            if user_id:
                order.append(as_text(user_id))
                seen.add(as_text(user_id))
            if scene == "group" and isinstance(participants, dict):
                for qq in reversed(list(participants.keys())):
                    qq = as_text(qq)
                    if qq and qq not in seen:
                        order.append(qq)
                        seen.add(qq)
                    if len(order) >= max_users:
                        break
            rows = []
            for qq in order[:max_users]:
                item = profiles.get(qq)
                if not isinstance(item, dict):
                    continue
                nickname = as_text(item.get("nickname")) or qq
                summary = as_text(item.get("summary"))[:240]
                notes = item.get("notes") if isinstance(item.get("notes"), list) else []
                note_text = "；".join(as_text(note)[:60] for note in notes[-3:])
                recent = item.get("recent") if isinstance(item.get("recent"), list) else []
                recent_text = "；".join(as_text(row.get("text"))[:40] for row in recent[-2:] if isinstance(row, dict))
                if not (summary or note_text or recent_text):
                    continue
                line = "- %s(%s)：" % (nickname, qq)
                if summary:
                    line += "印象：" + summary + " "
                if note_text:
                    line += "备注：" + note_text + " "
                if recent_text:
                    line += "最近：" + recent_text
                rows.append(line.strip())
        if not rows:
            return ""
        return ("【人物印象（长期背景，可能过时；只作关系/性格参考，不要用里面的具体事件或旧图判断当前图片/消息，当前内容优先）】" + chr(10) + chr(10).join(rows))

    def list_profiles(self, limit=20):
        store = self.service.state_store
        rows = []
        with store.lock:
            profiles = store.data.get("userProfiles") or {}
            for qq, item in profiles.items():
                if not isinstance(item, dict):
                    continue
                notes = item.get("notes") if isinstance(item.get("notes"), list) else []
                rows.append({"qq": as_text(qq), "nickname": as_text(item.get("nickname")) or as_text(qq),
                             "messages": safe_int(item.get("messages"), 0),
                             "summary": as_text(item.get("summary")),
                             "notes": [as_text(note) for note in notes[-3:]],
                             "lastSeen": safe_int(item.get("lastSeen"), 0)})
        rows.sort(key=lambda x: x.get("lastSeen", 0), reverse=True)
        return rows[:max(1, int(limit))]

    def affinity_for(self, user_id):
        store = self.service.state_store
        with store.lock:
            item = (store.data.get("userProfiles") or {}).get(as_text(user_id))
            if isinstance(item, dict) and item.get("affinity") is not None:
                return clamp_int(item.get("affinity"), 0, 100, 50)
        return 50

    def affinity_prompt(self, user_id):
        store = self.service.state_store
        with store.lock:
            item = (store.data.get("userProfiles") or {}).get(as_text(user_id))
            nickname = as_text(item.get("nickname")) if isinstance(item, dict) else ""
        score = self.affinity_for(user_id)
        if score >= 75:
            attitude = "很亲近、黏人，会傲娇关心，但仍嘴硬"
        elif score >= 50:
            attitude = "正常好感，嘴硬傲娇，心软不直说"
        elif score >= 25:
            attitude = "有点冷淡、敷衍，不太想接话"
        else:
            attitude = "明显冷淡、嫌弃，只回很短"
        return ("【好感度】你对 %s(%s) 的好感度：%d/100（%s）。请自然体现在语气里，"
                "不要直接提分数或这个设定。") % (nickname or as_text(user_id), as_text(user_id), score, attitude)

    def change_affinity(self, user_id, delta, reason=""):
        store = self.service.state_store
        delta = clamp_int(delta, -20, 20, 0)
        if delta == 0:
            return self.affinity_for(user_id)
        with store.lock:
            profiles = store.data.setdefault("userProfiles", {})
            key = as_text(user_id)
            item = profiles.get(key) if isinstance(profiles.get(key), dict) else {}
            current = clamp_int(item.get("affinity"), 0, 100, 50) if item.get("affinity") is not None else 50
            score = clamp_int(current + delta, 0, 100, 50)
            item["affinity"] = score
            history = item.get("affinityHistory") if isinstance(item.get("affinityHistory"), list) else []
            history.append({"delta": delta, "score": score, "at": int(time.time() * 1000), "reason": as_text(reason)[:100]})
            item["affinityHistory"] = history[-20:]
            item.setdefault("nickname", key)
            profiles[key] = item
            store.dirty = True
        self.service.log("好感度变化: qq=%s %+d -> %d (%s)" % (user_id, delta, score, reason))
        try:
            if hasattr(self.service, "memories"):
                self.service.memories.record_affinity_event(user_id, delta, reason)
        except Exception:
            pass
        return score

    def apply_affinity_rules(self, user_id, text, directed=True):
        config = self.service.config_store.get()
        profile_cfg = config.get("profiles") if is_object(config.get("profiles")) else {}
        if not profile_cfg.get("affinityRulesEnabled", True):
            return
        body = as_text(text).strip()
        if not body or not directed:
            return
        positive = ("喜欢你", "爱你", "可爱", "厉害", "谢谢", "乖", "抱抱", "贴贴", "聪明", "温柔", "棒", "夸夸", "好鱼", "小鲸鱼好")
        negative = ("傻逼", "煞笔", "滚", "闭嘴", "讨厌", "骗子", "废物", "蠢", "妈的", "草泥马", "你他妈", "恶心", "别烦", "嘴碎", "垃圾", "弱智")
        if any(word in body for word in negative):
            delta = -2
        elif any(word in body for word in positive):
            delta = 1
        else:
            return
        now = int(time.time() * 1000)
        cooldown = max(60, safe_int(profile_cfg.get("affinityRuleCooldownSeconds"), 900)) * 1000
        daily_cap = max(1, safe_int(profile_cfg.get("affinityRuleDailyCap"), 6))
        try:
            from zoneinfo import ZoneInfo
            day = datetime.now(ZoneInfo(first_non_blank(config.get("timeZone"), "Asia/Shanghai"))).strftime("%Y-%m-%d")
        except Exception:
            day = time.strftime("%Y-%m-%d")
        store = self.service.state_store
        with store.lock:
            profiles = store.data.setdefault("userProfiles", {})
            item = profiles.get(as_text(user_id)) if isinstance(profiles.get(as_text(user_id)), dict) else {}
            rules = item.get("affinityRules") if isinstance(item.get("affinityRules"), dict) else {}
            if as_text(rules.get("day")) != day:
                rules = {"day": day, "count": 0, "last": 0}
            if now - safe_int(rules.get("last"), 0) < cooldown or safe_int(rules.get("count"), 0) >= daily_cap:
                return
            rules["last"] = now
            rules["count"] = safe_int(rules.get("count"), 0) + 1
            item["affinityRules"] = rules
            profiles[as_text(user_id)] = item
            store.dirty = True
        self.change_affinity(user_id, delta, "聊天情绪")

    def set_affinity(self, user_id, score):
        current = self.affinity_for(user_id)
        return self.change_affinity(user_id, clamp_int(score, 0, 100, 50) - current, "手动设置")

    def view(self, user_id):
        store = self.service.state_store
        with store.lock:
            item = (store.data.get("userProfiles") or {}).get(as_text(user_id))
            return dict(item) if isinstance(item, dict) else None

    def add_note(self, user_id, note, nickname=""):
        store = self.service.state_store
        note = as_text(note).strip()[:200]
        if not note:
            return False
        with store.lock:
            profiles = store.data.setdefault("userProfiles", {})
            item = profiles.get(as_text(user_id)) if isinstance(profiles.get(as_text(user_id)), dict) else {"nickname": as_text(user_id), "messages": 0, "recent": [], "summary": "", "summaryAt": 0, "summaryCount": 0, "lastSeen": int(time.time() * 1000)}
            if nickname:
                item["nickname"] = as_text(nickname)[:60]
            notes = item.get("notes") if isinstance(item.get("notes"), list) else []
            notes.append(note)
            item["notes"] = notes[-20:]
            profiles[as_text(user_id)] = item
            store.dirty = True
        return True

    def clear(self, user_id):
        store = self.service.state_store
        with store.lock:
            profiles = store.data.setdefault("userProfiles", {})
            item = profiles.get(as_text(user_id))
            if not isinstance(item, dict):
                return False
            item["summary"] = ""
            item["notes"] = []
            item["recent"] = []
            item["summaryCount"] = safe_int(item.get("messages"), 0)
            item["summaryAt"] = int(time.time() * 1000)
            store.dirty = True
        return True




class MoodManager:
    """短期心情：0-100，单轴，随时间向基线衰减；只影响语气/主动性，不改人设。"""

    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()

    def _cfg(self):
        config = self.service.config_store.get()
        cfg = config.get("mood") if isinstance(config.get("mood"), dict) else {}
        return cfg

    def _read(self):
        cfg = self._cfg()
        now = time.time()
        baseline = clamp_int(cfg.get("baseline"), 0, 100, 50)
        decay = max(0.0, min(20.0, float(cfg.get("decayPerHour", 4))))
        initial = clamp_int(cfg.get("initial"), 0, 100, 55)
        min_score = clamp_int(cfg.get("minScore"), 0, 100, 5)
        max_score = clamp_int(cfg.get("maxScore"), 0, 100, 95)
        store = self.service.state_store
        with self.lock, store.lock:
            mood = store.data.get("mood") if isinstance(store.data.get("mood"), dict) else {}
            score = float(clamp_int(mood.get("score"), min_score, max_score, initial))
            updated_ms = safe_int(mood.get("updatedAt"), 0)
            if updated_ms > 0 and decay > 0:
                hours = max(0.0, min(720.0, (now - updated_ms / 1000.0) / 3600.0))
                if score > baseline:
                    score = max(baseline, score - decay * hours)
                elif score < baseline:
                    score = min(baseline, score + decay * hours)
            mood["score"] = int(round(clamp_int(score, min_score, max_score, initial)))
            mood["updatedAt"] = int(now * 1000)
            mood.setdefault("reason", "")
            mood.setdefault("history", [])
            store.data["mood"] = mood
            store.dirty = True
            return int(mood["score"]), mood, cfg

    @staticmethod
    def label_for(score):
        if score >= 80:
            return "开心/兴奋"
        if score >= 60:
            return "心情不错"
        if score >= 40:
            return "平静"
        if score >= 20:
            return "有点烦躁/低落"
        return "心情很差"

    def get(self):
        score, mood, _cfg = self._read()
        return {"score": score, "label": self.label_for(score), "reason": as_text(mood.get("reason")),
                "updatedAt": safe_int(mood.get("updatedAt"), 0)}

    def prompt(self):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return ""
        score, mood, _cfg = self._read()
        reason = as_text(mood.get("reason")).strip()
        anger = mood.get("angerAt") if isinstance(mood.get("angerAt"), dict) else {}
        anger_text = ""
        if anger and int(time.time() * 1000) - safe_int(anger.get("at"), 0) < 30 * 60 * 1000:
            anger_text = "；这股情绪主要冲着 QQ %s，不要迁怒当前说话的人" % as_text(anger.get("user_id"))
        return "【当前心情】%d/100（%s）%s%s。请自然体现在语气、长度和主动性上，不要直接报分数或解释这个设定。" % (
            score, self.label_for(score), ("，原因：" + reason) if reason else "", anger_text)

    def change(self, delta, reason=""):
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            return self.get()
        delta = max(-15, min(15, delta))
        if not delta:
            return self.get()
        score, mood, cfg = self._read()
        min_score = clamp_int(cfg.get("minScore"), 0, 100, 5)
        max_score = clamp_int(cfg.get("maxScore"), 0, 100, 95)
        new_score = clamp_int(score + delta, min_score, max_score, score)
        store = self.service.state_store
        with self.lock, store.lock:
            mood = store.data.get("mood") if isinstance(store.data.get("mood"), dict) else {}
            mood["score"] = new_score
            mood["reason"] = as_text(reason)[:100]
            mood["updatedAt"] = int(time.time() * 1000)
            history = mood.get("history") if isinstance(mood.get("history"), list) else []
            history.append({"delta": delta, "score": new_score, "at": int(time.time() * 1000), "reason": as_text(reason)[:60]})
            mood["history"] = history[-20:]
            store.data["mood"] = mood
            store.dirty = True
        self.service.log("心情变化: %+d -> %d (%s)" % (delta, new_score, reason))
        return self.get()

    def apply_rules(self, user_id, text, scene, affinity, directed=True):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return
        body = as_text(text).strip()
        if not body or not directed:
            return
        if re.search(r"别骂|不要骂|干嘛骂|为什么骂|护着|替他|替她|帮他|帮她|别欺负|别说了", body):
            return
        positive = ("喜欢", "爱你", "可爱", "厉害", "谢谢", "乖", "抱抱", "想你", "好看", "聪明", "棒", "温柔", "贴贴")
        negative = ("傻逼", "煞笔", "滚", "烦", "闭嘴", "讨厌", "骗子", "废物", "蠢", "妈的", "草泥马",
                    "你他妈", "恶心", "别烦", "吊样", "嘴碎", "什么东西", "扫码", "禁言", "滚蛋",
                    "傻", "垃圾", "弱智", "去死", "丑")
        delta = 0
        reasons = []
        if any(word in body for word in negative):
            delta -= 4 if affinity < 50 else 5
            reasons.append("被骂")
        elif any(word in body for word in positive):
            delta += 2 if affinity >= 50 else 1
            reasons.append("被夸")
        try:
            from zoneinfo import ZoneInfo
            hour = datetime.now(ZoneInfo(first_non_blank(self.service.config_store.get().get("timeZone"), "Asia/Shanghai"))).hour
        except Exception:
            hour = time.localtime().tm_hour
        if 0 <= hour < 6 and delta >= 0:
            delta -= 1
            reasons.append("深夜")
        if not delta:
            return
        now = int(time.time() * 1000)
        cooldown = max(10, safe_int(cfg.get("ruleCooldownSeconds"), 60)) * 1000
        store = self.service.state_store
        with self.lock, store.lock:
            mood = store.data.get("mood") if isinstance(store.data.get("mood"), dict) else {}
            last_rules = mood.get("lastRuleAt") if isinstance(mood.get("lastRuleAt"), dict) else {}
            last_at = safe_int(last_rules.get(as_text(user_id)), 0)
            if now - last_at < cooldown:
                return
            last_rules[as_text(user_id)] = now
            mood["lastRuleAt"] = last_rules
            store.data["mood"] = mood
            store.dirty = True
        self.change(delta, "、".join(reasons))
        try:
            store = self.service.state_store
            with self.lock, store.lock:
                mood = store.data.get("mood") if isinstance(store.data.get("mood"), dict) else {}
                if delta < 0:
                    mood["angerAt"] = {"user_id": as_text(user_id), "at": int(time.time() * 1000)}
                elif delta > 0:
                    mood.pop("angerAt", None)
                store.data["mood"] = mood
                store.dirty = True
        except Exception:
            pass

    def proactive_allowed(self):
        cfg = self._cfg()
        if not cfg.get("enabled", True) or not cfg.get("affectProactive", True):
            return True
        low = clamp_int(cfg.get("proactiveLowThreshold"), 0, 100, 30)
        score = self.get().get("score", 50)
        return score >= low




class VoiceService:
    """TTS 语音：Edge TTS 生成小鲸鱼声线；按好感/心情/频率限制。"""

    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()

    def _cfg(self):
        config = self.service.config_store.get()
        return config.get("voice") if isinstance(config.get("voice"), dict) else {}

    def _is_owner(self, user_id):
        config = self.service.config_store.get()
        perms = config.get("permissions") if isinstance(config.get("permissions"), dict) else {}
        return as_text(user_id) in [as_text(item) for item in (perms.get("ownerUsers") or [])]

    def _familiarity(self, user_id, chat_key):
        try:
            scene = "group" if as_text(chat_key).startswith("group:") else "private"
            state = self.service.relationships.state_for(scene, chat_key, user_id)
            return int(state.get("familiarity", 50))
        except Exception:
            return 50

    def _mood(self):
        try:
            return self.service.mood.get().get("score", 50)
        except Exception:
            return 50

    def _today(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(first_non_blank(self.service.config_store.get().get("timeZone"), "Asia/Shanghai"))).strftime("%Y-%m-%d")
        except Exception:
            return time.strftime("%Y-%m-%d")

    def _count(self, chat_key):
        store = self.service.state_store
        with store.lock:
            counters = store.data.get("voiceCounters") if isinstance(store.data.get("voiceCounters"), dict) else {}
            item = counters.get(chat_key) if isinstance(counters.get(chat_key), dict) else {}
            if as_text(item.get("day")) != self._today():
                return 0
            return safe_int(item.get("count"), 0)

    def _incr(self, chat_key):
        store = self.service.state_store
        day = self._today()
        with store.lock:
            counters = store.data.setdefault("voiceCounters", {})
            item = counters.get(chat_key) if isinstance(counters.get(chat_key), dict) else {}
            if as_text(item.get("day")) != day:
                item = {"day": day, "count": 0}
            item["count"] = safe_int(item.get("count"), 0) + 1
            counters[chat_key] = item
            store.dirty = True

    def can_use(self, user_id, chat_key, auto=True):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return False, "语音功能没开"
        if self._is_owner(user_id):
            return True, ""
        if cfg.get("enabled", True) is False:
            return False, "语音功能没开"
        threshold = safe_int(cfg.get("autoMinFamiliarity", cfg.get("autoMinAffinity", 60)), 60) if auto             else safe_int(cfg.get("requestFamiliarity", cfg.get("requestAffinity", 70)), 70)
        if self._familiarity(user_id, chat_key) < threshold:
            return False, "关系还不够熟，暂时不想发语音"
        if self._mood() < safe_int(cfg.get("moodMin"), 30):
            return False, "今天心情不好，不想发语音"
        if self._count(chat_key) >= safe_int(cfg.get("maxPerDay"), 3):
            return False, "今天语音额度用完了"
        return True, ""

    def speak(self, scene, target, text, user_id, chat_key, auto=True):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return {"ok": False, "error": "语音功能没开"}
        body = " ".join(as_text(text).split())[:max(5, safe_int(cfg.get("maxChars"), 60))]
        if not body:
            return {"ok": False, "error": "语音内容为空"}
        owner = self._is_owner(user_id)
        if not owner:
            allowed, reason = self.can_use(user_id, chat_key, auto=auto)
            if not allowed:
                return {"ok": False, "error": reason}
        voice = first_non_blank(cfg.get("voice"), "zh-CN-XiaoyiNeural")
        edge_bin = first_non_blank(cfg.get("edgeTtsBin"), "/home/ubuntu/.local/bin/edge-tts")
        if not os.path.isfile(edge_bin):
            edge_bin = shutil.which("edge-tts") or ""
        if not edge_bin:
            return {"ok": False, "error": "edge-tts 未安装"}
        safe_chat = re.sub(r"[^0-9A-Za-z_.-]+", "_", as_text(chat_key))[:40] or "chat"
        host_dir = os.path.join("/opt/snowluma/data/voice", safe_chat)
        os.makedirs(host_dir, exist_ok=True)
        stamp = int(time.time() * 1000)
        host_path = os.path.join(host_dir, "v%d.mp3" % stamp)
        cmd = [edge_bin, "--voice", voice, "--text", body, "--write-media", host_path]
        rate = as_text(cfg.get("rate")).strip()
        pitch = as_text(cfg.get("pitch")).strip()
        if rate:
            cmd.extend(["--rate", rate])
        if pitch:
            cmd.extend(["--pitch", pitch])
        try:
            completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except Exception as error:
            return {"ok": False, "error": "TTS 执行失败: %s: %s" % (error.__class__.__name__, error)}
        if completed.returncode != 0 or not os.path.isfile(host_path) or os.path.getsize(host_path) < 1000:
            detail = (completed.stderr or b"").decode("utf-8", "replace")[-200:]
            return {"ok": False, "error": "TTS 生成失败: " + detail}
        app_path = "/app/data/voice/" + safe_chat + "/" + os.path.basename(host_path)
        segment = [{"type": "record", "data": {"file": "file://" + app_path}}]
        action = "send_private_msg" if scene == "private" else "send_group_msg"
        key = "user_id" if scene == "private" else "group_id"
        params = {key: target, "message": segment}
        try:
            data = self.service.bridge.call_action(action, params, timeout_ms=30000)
        except Exception as error:
            return {"ok": False, "error": "发送语音失败: %s: %s" % (error.__class__.__name__, error)}
        if not owner:
            self._incr(chat_key)
        message_id = as_text((data or {}).get("message_id")) if isinstance(data, dict) else ""
        self.service.log("语音已发送: %s %s 字数=%d" % (scene, target, len(body)))
        return {"ok": True, "message_id": message_id, "bytes": os.path.getsize(host_path)}

    def cleanup(self):
        root = "/opt/snowluma/data/voice"
        now = time.time()
        try:
            for name in os.listdir(root):
                folder = os.path.join(root, name)
                if not os.path.isdir(folder):
                    continue
                for child in os.listdir(folder):
                    path = os.path.join(folder, child)
                    try:
                        if os.path.isfile(path) and now - os.path.getmtime(path) > 24 * 3600:
                            os.remove(path)
                    except OSError:
                        pass
        except OSError:
            pass




class SleepManager:
    """23:00-07:00 睡眠：少量消息静默到早上补回，超过阈值被吵醒并带起床气。"""

    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="snowluma-sleep", daemon=True)
        self.thread.start()

    def _cfg(self):
        config = self.service.config_store.get()
        return config.get("sleep") if isinstance(config.get("sleep"), dict) else {}

    def _now(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(first_non_blank(self.service.config_store.get().get("timeZone"), "Asia/Shanghai")))
        except Exception:
            return datetime.now()

    def _minutes(self, value, default):
        try:
            hour, minute = first_non_blank(value, default).split(":")[:2]
            return max(0, min(1439, int(hour) * 60 + int(minute)))
        except Exception:
            return default

    def _sleep_window(self, now_dt=None):
        cfg = self._cfg()
        now_dt = now_dt or self._now()
        start = self._minutes(cfg.get("start"), 23 * 60)
        end = self._minutes(cfg.get("end"), 7 * 60)
        current = now_dt.hour * 60 + now_dt.minute
        if start == end:
            return False, start, end, current
        if start < end:
            inside = start <= current < end
        else:
            inside = current >= start or current < end
        return inside, start, end, current

    def is_sleeping(self):
        return self._sleep_window()[0]

    def _session(self, now_dt, start, end):
        if start < end:
            return now_dt.strftime("%Y-%m-%d")
        if now_dt.hour * 60 + now_dt.minute < end:
            from datetime import timedelta
            return (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        return now_dt.strftime("%Y-%m-%d")

    def handle(self, event, scene, target, chat_key, user_id, text):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return "normal"
        # Owner's private chat is 24/7: no sleep hold, no morning summary.
        if scene == "private" and cfg.get("ownerPrivateExempt", True):
            try:
                owners = self._owners(self.service.config_store.get())
                if as_text(user_id) in owners:
                    with self.service.state_store.lock:
                        (self.service.state_store.data.get("sleepBuffers") or {}).pop(chat_key, None)
                        self.service.state_store.dirty = True
                    return "normal"
            except Exception:
                pass
        now_dt = self._now()
        inside, start, end, current = self._sleep_window(now_dt)
        if not inside:
            return "normal"
        threshold = clamp_int(cfg.get("wakeThreshold"), 1, 100, 8)
        max_buffer = clamp_int(cfg.get("maxBuffer"), 10, 200, 40)
        session = self._session(now_dt, start, end)
        store = self.service.state_store
        with store.lock:
            buffers = store.data.setdefault("sleepBuffers", {})
            item = buffers.get(chat_key) if isinstance(buffers.get(chat_key), dict) else {}
            if as_text(item.get("session")) != session:
                item = {"session": session, "scene": scene, "target": target, "messages": [], "woken": False, "updatedAt": 0}
            messages = item.get("messages") if isinstance(item.get("messages"), list) else []
            messages.append({"user_id": as_text(user_id), "nickname": as_text(event.get("sender_name")) or as_text(user_id),
                             "text": as_text(text)[:500], "at": now_ms()})
            item["messages"] = messages[-max_buffer:]
            item["updatedAt"] = now_ms()
            item["scene"] = scene
            item["target"] = target
            if item.get("woken"):
                action = "awake"
            elif len(item["messages"]) > threshold:
                item["woken"] = True
                action = "wake"
            else:
                action = "hold"
            buffers[chat_key] = item
            store.dirty = True
            return action

    def has_pending(self, chat_key):
        with self.service.state_store.lock:
            buffers = self.service.state_store.data.get("sleepBuffers") or {}
            item = buffers.get(chat_key)
            return bool(isinstance(item, dict) and item.get("messages") and not item.get("woken"))

    def _set_wake_cooldown(self, chat_key):
        cfg = self._cfg()
        minutes = clamp_int(cfg.get("postWakeQuietMinutes"), 0, 1440, 30)
        if not minutes:
            return
        until = int(self._now().timestamp() * 1000) + minutes * 60 * 1000
        with self.service.state_store.lock:
            data = self.service.state_store.data.setdefault("sleepCooldownUntil", {})
            data[chat_key] = until
            self.service.state_store.dirty = True

    def get_buffer(self, chat_key):
        with self.service.state_store.lock:
            buffers = self.service.state_store.data.get("sleepBuffers") or {}
            item = buffers.get(chat_key)
            return dict(item) if isinstance(item, dict) else {}

    def _owners(self, config):
        perms = config.get("permissions") if isinstance(config.get("permissions"), dict) else {}
        return [as_text(item) for item in (perms.get("ownerUsers") or [])]

    def wake(self, chat_key, config):
        cfg = self._cfg()
        item = self.get_buffer(chat_key)
        scene = as_text(item.get("scene"))
        target = as_text(item.get("target"))
        messages = item.get("messages") if isinstance(item.get("messages"), list) else []
        last = messages[-1] if messages else {}
        user_id = as_text(last.get("user_id"))
        lines = []
        for row in messages[-12:]:
            if isinstance(row, dict):
                lines.append("- %s：%s" % (as_text(row.get("nickname")), as_text(row.get("text"))[:100]))
        system_prompt = skills_ext.build_system_prompt(self.service, config, chat_key, scene, user_id=user_id)
        system_prompt += ("\n\n【睡眠被打断】现在是 23:00-07:00 的睡眠时间，你被第 %d 条夜猫子消息吵醒，"
                          "有轻微起床气。回复要短、带困意、吐槽对方夜猫子/几点了，可以小脾气但不恶意攻击；不要长篇。") % len(messages)
        user_content = "[夜里收到的消息]\n" + "\n".join(lines[-12:])
        effort = "max" if (scene == "private" and user_id in self._owners(config)) else "low"
        try:
            result = call_deepseek(config, [{"role": "system", "content": system_prompt},
                                            {"role": "user", "content": user_content}], effort_override=effort)
            if result.get("ok") and as_text(result.get("reply")).strip():
                reply = as_text(result.get("reply")).strip()[:300]
                outcome = skills_ext.deliver_reply(self.service, {"user_id": user_id}, scene, target, chat_key, reply,
                                                    config, allow_auto_at=False)
                if outcome.get("ok") and not outcome.get("silent"):
                    self.service.state_store.incr("processed")
                    self.service.state_store.incr("replied")
                    self.service.log("睡眠被打断，已起床气回复: chat=%s" % chat_key)
                    self._set_wake_cooldown(chat_key)
                    self.service.auto_engine._record_daily_assistant(chat_key, reply, source="sleep_wake")
        except Exception as error:
            self.service.log("起床气回复失败: %s" % error)
        with self.service.state_store.lock:
            buffers = self.service.state_store.data.setdefault("sleepBuffers", {})
            current = buffers.get(chat_key) if isinstance(buffers.get(chat_key), dict) else {}
            current["messages"] = []
            current["woken"] = True
            buffers[chat_key] = current
            self.service.state_store.dirty = True

    def _flush_chat(self, chat_key, item, config, date_key=""):
        cfg = self._cfg()
        if not cfg.get("summaryEnabled", True) or not item.get("messages"):
            return True
        scene = as_text(item.get("scene"))
        target = as_text(item.get("target"))
        messages = item.get("messages") if isinstance(item.get("messages"), list) else []
        last = messages[-1] if messages else {}
        user_id = as_text(last.get("user_id"))
        lines = []
        for row in messages[-15:]:
            if isinstance(row, dict):
                lines.append("- %s：%s" % (as_text(row.get("nickname")), as_text(row.get("text"))[:120]))
        system_prompt = skills_ext.build_system_prompt(self.service, config, chat_key, scene, user_id=user_id)
        system_prompt += ("\n\n【早晨补回】现在是早上，你刚睡醒，发现夜里有人发了消息。用简短、带困意和吐槽夜猫子的语气回应一下，"
                          "可以挑一两条内容接，不要逐条汇报，不要说客服式的话。")
        user_content = "[夜里的消息]\n" + "\n".join(lines[-15:])
        effort = "max" if (scene == "private" and user_id in self._owners(config)) else "low"
        try:
            result = call_deepseek(config, [{"role": "system", "content": system_prompt},
                                            {"role": "user", "content": user_content}], effort_override=effort)
            if result.get("ok") and as_text(result.get("reply")).strip():
                reply = as_text(result.get("reply")).strip()[:500]
                outcome = skills_ext.deliver_reply(self.service, {"user_id": user_id}, scene, target, chat_key, reply,
                                                    config, allow_auto_at=False)
                if outcome.get("ok") and not outcome.get("silent"):
                    self.service.state_store.incr("replied")
                    self.service.log("早晨补回已发送: chat=%s 消息数=%d" % (chat_key, len(messages)))
                    with self.service.state_store.lock:
                        buffers = self.service.state_store.data.setdefault("sleepBuffers", {})
                        buffers.pop(chat_key, None)
                        if date_key:
                            flushes = self.service.state_store.data.setdefault("sleepFlushedDates", {})
                            flushes[chat_key] = date_key
                        self.service.state_store.dirty = True
                    self.service.auto_engine._record_daily_assistant(chat_key, reply, source="sleep_flush")
                    # If cooldown formatting fails, never resend the same morning summary.
                    try:
                        self._set_wake_cooldown(chat_key)
                    except Exception as cooldown_error:
                        self.service.log("设置起床冷却失败（已去重）: %s" % cooldown_error)
                    return True
        except Exception as error:
            self.service.log("早晨补回失败: %s" % error)
            return False
        return False

    def flush(self):
        config = self.service.config_store.get()
        now_dt = self._now()
        inside, start, end, current = self._sleep_window(now_dt)
        if inside:
            return
        date_key = now_dt.strftime("%Y-%m-%d")
        if not self._cfg().get("summaryEnabled", True):
            with self.service.state_store.lock:
                self.service.state_store.data["sleepBuffers"] = {}
                self.service.state_store.dirty = True
            return
        with self.service.state_store.lock:
            buffers = self.service.state_store.data.get("sleepBuffers") or {}
            flushed = self.service.state_store.data.get("sleepFlushedDates") or {}
            attempts = self.service.state_store.data.get("sleepFlushAttempts") or {}
            snapshot = [(key, dict(value)) for key, value in buffers.items() if isinstance(value, dict)]
        max_attempts = clamp_int(self._cfg().get("flushMaxAttempts"), 1, 10, 3)
        for chat_key, item in snapshot:
            if item.get("woken") or as_text(flushed.get(chat_key)) == date_key:
                with self.service.state_store.lock:
                    self.service.state_store.data.setdefault("sleepBuffers", {}).pop(chat_key, None)
                    self.service.state_store.dirty = True
                continue
            attempt = attempts.get(chat_key) if isinstance(attempts.get(chat_key), dict) else {}
            if as_text(attempt.get("session")) != as_text(item.get("session")):
                attempt = {"session": as_text(item.get("session")), "count": 0, "last": 0}
            if safe_int(attempt.get("count"), 0) >= max_attempts:
                with self.service.state_store.lock:
                    self.service.state_store.data.setdefault("sleepBuffers", {}).pop(chat_key, None)
                    self.service.state_store.data.setdefault("sleepFlushedDates", {})[chat_key] = date_key
                    self.service.state_store.dirty = True
                self.service.log("早晨补回超过重试上限，已丢弃: chat=%s" % chat_key)
                continue
            ok = self._flush_chat(chat_key, item, config, date_key)
            if not ok:
                count = safe_int(attempt.get("count"), 0) + 1
                with self.service.state_store.lock:
                    self.service.state_store.data.setdefault("sleepFlushAttempts", {})[chat_key] = {
                        "session": as_text(item.get("session")), "count": count, "last": now_ms()}
                    if count >= max_attempts:
                        self.service.state_store.data.setdefault("sleepBuffers", {}).pop(chat_key, None)
                        self.service.state_store.data.setdefault("sleepFlushedDates", {})[chat_key] = date_key
                    self.service.state_store.dirty = True
                if count >= max_attempts:
                    self.service.log("早晨补回失败达到上限，已丢弃: chat=%s" % chat_key)
                    try:
                        self.service.monitor.record_error("sleep_flush", "早晨补回失败达到上限: %s" % chat_key)
                    except Exception:
                        pass

    def _loop(self):
        while not self.stop_event.is_set():
            time.sleep(20)
            try:
                self.flush()
            except Exception as error:
                try:
                    self.service.log("睡眠缓冲处理失败: %s" % error)
                except Exception:
                    pass




def pricing_off_peak(config):
    """官方峰谷：北京时间周一至周五（非法定节假日）9:00-12:00、14:00-18:00 为高峰；
    其余时间、周末、法定节假日全天为空闲。"""
    pricing = config.get("pricing") if isinstance(config.get("pricing"), dict) else {}
    if not pricing.get("enabled", True):
        return True
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(first_non_blank(config.get("timeZone"), "Asia/Shanghai")))
    except Exception:
        now = datetime.now()

    def to_minutes(value, default):
        try:
            hour, minute = as_text(value).split(":")[:2]
            return max(0, min(1439, int(hour) * 60 + int(minute)))
        except Exception:
            return default

    weekdays = []
    for item in (pricing.get("peakWeekdays") or [0, 1, 2, 3, 4]):
        try:
            weekdays.append(int(item))
        except (TypeError, ValueError):
            pass
    if now.weekday() not in weekdays:
        return True
    if now.strftime("%Y-%m-%d") in [as_text(item) for item in (pricing.get("holidayDates") or [])]:
        return True
    current = now.hour * 60 + now.minute
    for window in (pricing.get("peakWindows") or []):
        parts = as_text(window).split("-")
        if len(parts) != 2:
            continue
        start = to_minutes(parts[0], -1)
        end = to_minutes(parts[1], -1)
        if start < 0 or end < 0 or start == end:
            continue
        if start < end and start <= current < end:
            return False
        if start > end and (current >= start or current < end):
            return False
    return True


class MemoryManager:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.counters = {}
        self.last_extract = {}

    def _cfg(self):
        config = self.service.config_store.get()
        return config.get("memory") if isinstance(config.get("memory"), dict) else {}

    def _db_path(self):
        return os.path.join(getattr(self.service, "state_dir", "/opt/snowluma/bridge"), "memory.db")

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self._db_path(), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        return conn

    def _ensure_schema(self):
        conn = self._connect()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL, scope_id TEXT NOT NULL, subject_qq TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'fact', content TEXT NOT NULL,
                importance INTEGER NOT NULL DEFAULT 50, confidence INTEGER NOT NULL DEFAULT 80,
                pinned INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'auto',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                last_access_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER,
                access_count INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(scope, scope_id, subject_qq, content_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, scope_id, active, expires_at);
            CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS image_memes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL UNIQUE, dhash TEXT NOT NULL DEFAULT '',
                meaning TEXT NOT NULL, source_qq TEXT NOT NULL DEFAULT '',
                chat_key TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            """)
            conn.commit()
        finally:
            conn.close()

    def start(self):
        self._ensure_schema()
        self._migrate_profiles()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, name="snowluma-memory", daemon=True)
            self.thread.start()

    def image_meme_prompt(self, fingerprint):
        if not isinstance(fingerprint, dict):
            return ""
        sha = as_text(fingerprint.get("sha256"))
        dhash = as_text(fingerprint.get("dhash")).lower()
        if not sha:
            return ""
        conn = self._connect()
        try:
            row = conn.execute("SELECT meaning FROM image_memes WHERE sha256=?", (sha,)).fetchone()
            if not row and dhash:
                rows = conn.execute("SELECT dhash, meaning FROM image_memes WHERE dhash<>''").fetchall()
                best = None
                best_distance = 7
                try:
                    target = int(dhash, 16)
                except ValueError:
                    target = None
                if target is not None:
                    for item in rows:
                        try:
                            distance = bin(target ^ int(as_text(item["dhash"]), 16)).count("1")
                        except ValueError:
                            continue
                        if distance < best_distance:
                            best_distance = distance
                            best = as_text(item["meaning"])
                if best:
                    return "这张图之前被解释为：%s" % best[:200]
            return ("这张图之前被解释为：%s" % as_text(row["meaning"])[:200]) if row else ""
        finally:
            conn.close()

    def set_pending_image(self, chat_key, fingerprint, source_qq):
        if not isinstance(fingerprint, dict) or not as_text(fingerprint.get("sha256")):
            return
        with self.service.state_store.lock:
            data = self.service.state_store.data.setdefault("pendingImage", {})
            data[chat_key] = {"sha256": as_text(fingerprint.get("sha256")),
                              "dhash": as_text(fingerprint.get("dhash")),
                              "source_qq": as_text(source_qq),
                              "at": self._now()}
            self.service.state_store.dirty = True

    def learn_pending_image(self, chat_key, text, source_qq):
        body = re.sub(r"@(?:你|\d+)\s*", "", as_text(text)).strip()
        if len(body) < 2 or len(body) > 200:
            return ""
        if "按他说的" in body or body in ("记住", "记", "记了", "记一下"):
            return ""
        with self.service.state_store.lock:
            pending = (self.service.state_store.data.get("pendingImage") or {}).get(chat_key)
            pending = dict(pending) if isinstance(pending, dict) else None
        if not pending or self._now() - int(pending.get("at") or 0) > 10 * 60 * 1000:
            return ""
        signals = ("=", "就是", "意思", "代表", "指的是", "我懂", "表示", "用来")
        named = ("记住", "记一下", "这是", "叫", "梗", "图")
        if not any(word in body for word in signals):
            if not ("记住" in body and any(word in body for word in ("梗", "这是", "叫", "意思", "图"))):
                return ""
        conn = self._connect()
        try:
            with conn:
                conn.execute("""INSERT INTO image_memes(sha256, dhash, meaning, source_qq, chat_key, created_at, updated_at)
                                VALUES(?,?,?,?,?,?,?)
                                ON CONFLICT(sha256) DO UPDATE SET meaning=excluded.meaning, dhash=excluded.dhash,
                                source_qq=excluded.source_qq, chat_key=excluded.chat_key, updated_at=excluded.updated_at""",
                             (as_text(pending.get("sha256")), as_text(pending.get("dhash")), body[:200],
                              as_text(source_qq), as_text(chat_key), self._now(), self._now()))
        except Exception as error:
            self.service.log("图片梗记录失败: %s" % error)
            return ""
        finally:
            conn.close()
        with self.service.state_store.lock:
            (self.service.state_store.data.get("pendingImage") or {}).pop(chat_key, None)
            self.service.state_store.dirty = True
        self.service.log("图片梗已记录: %s" % body[:80])
        return body

    def _migrate_profiles(self):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM memory_meta WHERE key='migrated_profiles_v1'").fetchone()
            if row:
                return
        finally:
            conn.close()
        try:
            with self.service.state_store.lock:
                profiles = self.service.state_store.data.get("userProfiles") or {}
            for qq, item in profiles.items():
                if not isinstance(item, dict):
                    continue
                qq = as_text(qq)
                summary = as_text(item.get("summary")).strip()
                if summary:
                    self.add("user", qq, qq, summary, "fact", 65, 80, False, "profile")
                for note in (item.get("notes") or []):
                    note = as_text(note).strip()
                    if note:
                        self.add("user", qq, qq, note, "fact", 78, 85, False, "manual")
        except Exception as error:
            self.service.log("迁移印象失败: %s" % error)
        conn = self._connect()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES('migrated_profiles_v1', ?)",
                             (str(self._now()),))
        finally:
            conn.close()

    def _now(self):
        return int(time.time() * 1000)

    def _expiry_for(self, importance, pinned):
        if pinned or importance >= 80:
            return None
        days = 180 if importance >= 40 else 7
        return self._now() + days * 24 * 3600 * 1000

    def _normalize(self, content):
        return re.sub(r"\s+", " ", as_text(content).strip())[:500]

    def add(self, scope, scope_id, subject_qq, content, kind="fact", importance=50, confidence=80, pinned=False, source="auto"):
        content = self._normalize(content)
        if len(content) < 3:
            return None
        importance = clamp_int(importance, 0, 100, 50)
        confidence = clamp_int(confidence, 0, 100, 80)
        now = self._now()
        expires = self._expiry_for(importance, pinned)
        digest = hashlib.sha256(("%s|%s|%s|%s" % (scope, scope_id, subject_qq, content)).encode("utf-8")).hexdigest()
        conn = self._connect()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO memories(scope, scope_id, subject_qq, kind, content, importance, confidence,
                        pinned, source, created_at, updated_at, last_access_at, expires_at, access_count, content_hash, active)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(scope, scope_id, subject_qq, content_hash) DO UPDATE SET
                        importance=MAX(memories.importance, excluded.importance),
                        confidence=MAX(memories.confidence, excluded.confidence),
                        pinned=MAX(memories.pinned, excluded.pinned),
                        updated_at=excluded.updated_at,
                        expires_at=CASE WHEN MAX(memories.pinned, excluded.pinned)=1 THEN NULL
                            WHEN MAX(memories.importance, excluded.importance)>=80 THEN NULL
                            ELSE excluded.expires_at END,
                        active=1
                """, (scope, scope_id, subject_qq, kind, content, importance, confidence, 1 if pinned else 0,
                      source, now, now, 0, expires, 0, digest))
                row = conn.execute("SELECT id FROM memories WHERE scope=? AND scope_id=? AND subject_qq=? AND content_hash=?",
                                   (scope, scope_id, subject_qq, digest)).fetchone()
            return row["id"] if row else None
        except Exception as error:
            self.service.log("写入记忆失败: %s" % error)
            return None
        finally:
            conn.close()


    def cleanup(self):
        cutoff = self._now()
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE memories SET active=0 WHERE pinned=0 AND expires_at IS NOT NULL AND expires_at < ?", (cutoff,))
            return True
        finally:
            conn.close()

    def list_image_memes(self, query="", limit=20):
        conn = self._connect()
        try:
            params = []
            sql = "SELECT * FROM image_memes"
            if query:
                sql += " WHERE meaning LIKE ?"
                params.append("%" + query + "%")
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(max(1, min(100, int(limit))))
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def list_memories(self, scope, scope_id, query="", limit=20):
        conn = self._connect()
        try:
            clauses = ["scope=?", "scope_id=?", "active=1"]
            params = [scope, scope_id]
            if query:
                clauses.append("content LIKE ?")
                params.append("%" + query + "%")
            rows = conn.execute("SELECT * FROM memories WHERE " + " AND ".join(clauses) +
                                " ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?",
                                params + [max(1, min(100, int(limit)))]).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def search(self, scope, scope_id, query, limit=10):
        return self.list_memories(scope, scope_id, query, limit)

    def pin(self, memory_id, pinned=True):
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE memories SET pinned=?, expires_at=NULL, importance=MAX(importance, 85) WHERE id=?",
                             (1 if pinned else 0, int(memory_id)))
            return True
        finally:
            conn.close()

    def forget(self, memory_id):
        conn = self._connect()
        try:
            with conn:
                conn.execute("UPDATE memories SET active=0 WHERE id=?", (int(memory_id),))
            return True
        finally:
            conn.close()

    def _tokens(self, text):
        body = re.sub(r"[^\w\u4e00-\u9fff]+", " ", as_text(text))
        chunks = [chunk for chunk in body.split() if len(chunk) >= 2]
        tokens = []
        for chunk in chunks[:20]:
            if len(chunk) <= 4:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i:i + 2] for i in range(0, min(len(chunk) - 1, 12), 2))
        return list(dict.fromkeys(tokens))[:30]

    def retrieve(self, scene, chat_key, user_id, text):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return []
        max_items = clamp_int(cfg.get("maxPromptItems"), 1, 20, 6)
        now = self._now()
        scopes = [("user", as_text(user_id))]
        if scene == "group" and chat_key.startswith("group:"):
            scopes.append(("group", chat_key.split(":", 1)[1]))
        conn = self._connect()
        try:
            rows = []
            for scope, scope_id in scopes:
                rows.extend(conn.execute(
                    "SELECT * FROM memories WHERE scope=? AND scope_id=? AND active=1 AND (expires_at IS NULL OR expires_at>?)",
                    (scope, scope_id, now)).fetchall())
        except Exception as error:
            self.service.log("检索记忆失败: %s" % error)
            return []
        finally:
            conn.close()
        tokens = self._tokens(text)
        scored = []
        for row in rows:
            item = dict(row)
            content = as_text(item.get("content"))
            matches = sum(1 for token in tokens if token in content)
            score = int(item.get("importance") or 0) * 0.6 + matches * 12
            if item.get("pinned"):
                score += 100
            if item.get("scope") == "group":
                score += 3
            if item.get("last_access_at"):
                age_days = max(0, (now - int(item.get("last_access_at"))) / 86400000.0)
                score += max(0, 8 - age_days)
            if matches == 0 and not item.get("pinned") and int(item.get("importance") or 0) < 70:
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = [item for _score, item in scored[:max_items]]
        ids = [item["id"] for item in chosen]
        if ids:
            conn = self._connect()
            try:
                with conn:
                    for memory_id in ids:
                        conn.execute("UPDATE memories SET last_access_at=?, access_count=access_count+1 WHERE id=?", (now, memory_id))
            finally:
                conn.close()
        return chosen

    def prompt_for(self, scene, chat_key, user_id, text):
        items = self.retrieve(scene, chat_key, user_id, text)
        lines = []
        for item in items:
            content = as_text(item.get("content")).strip()
            if content:
                lines.append("- " + content[:240])
        if not lines:
            return ""
        return "[长期记忆（可能过时，仅供参考）]\n" + "\n".join(lines)

    def record_affinity_event(self, user_id, delta, reason=""):
        if not user_id or not delta:
            return
        try:
            from zoneinfo import ZoneInfo
            day = datetime.now(ZoneInfo(first_non_blank(self.service.config_store.get().get("timeZone"), "Asia/Shanghai"))).strftime("%Y-%m-%d")
        except Exception:
            day = time.strftime("%Y-%m-%d")
        content = "%s 对 %s 的好感 %+d（%s）" % (day, as_text(user_id), int(delta), as_text(reason)[:60])
        self.add("user", as_text(user_id), as_text(user_id), content, "event", 55 if abs(int(delta)) >= 2 else 42, 80, False, "affinity")


    def consider(self, chat_key, scene, target, user_id, text):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return
        every = clamp_int(cfg.get("extractEvery"), 3, 100, 8)
        cooldown = clamp_int(cfg.get("extractCooldownMinutes"), 1, 1440, 10) * 60 * 1000
        self.counters[chat_key] = self.counters.get(chat_key, 0) + 1
        if self.counters[chat_key] % every != 0:
            return
        if self._now() - self.last_extract.get(chat_key, 0) < cooldown:
            return
        self.last_extract[chat_key] = self._now()
        self.queue.put({"chat_key": chat_key, "scene": scene, "target": target, "user_id": user_id, "text": text})

    def _loop(self):
        while not self.stop_event.is_set():
            item = None
            try:
                item = self.queue.get(timeout=5)
            except queue.Empty:
                item = None
            if item is not None:
                try:
                    self._extract(item)
                except Exception as error:
                    self.service.log("记忆抽取失败: %s" % error)
            try:
                self.cleanup()
            except Exception:
                pass
            try:
                self._maybe_consolidate()
            except Exception as error:
                self.service.log("每日记忆整理检查失败: %s" % error)


    def force_extract(self, chat_key, scene, target, user_id, text):
        self.queue.put({"chat_key": chat_key, "scene": scene, "target": target, "user_id": user_id, "text": text})
        return True

    def _meta_get(self, key):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM memory_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else ""
        finally:
            conn.close()

    def _meta_set(self, key, value):
        conn = self._connect()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES(?, ?)", (key, as_text(value)))
        finally:
            conn.close()

    def consolidate(self):
        try:
            self.cleanup()
        except Exception:
            pass
        now = self._now()
        conn = self._connect()
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM memories WHERE active=1 AND (expires_at IS NULL OR expires_at>?)", (now,)).fetchall()]
        finally:
            conn.close()
        groups = {}
        for row in rows:
            key = (as_text(row.get("scope")), as_text(row.get("scope_id")), as_text(row.get("subject_qq")))
            groups.setdefault(key, []).append(row)
        merged = 0
        try:
            from difflib import SequenceMatcher
        except Exception:
            SequenceMatcher = None
        conn = self._connect()
        try:
            with conn:
                for _key, items in groups.items():
                    items.sort(key=lambda row: (int(row.get("pinned") or 0), int(row.get("importance") or 0)), reverse=True)
                    for index, left in enumerate(items):
                        if not left.get("active", 1):
                            continue
                        for right in items[index + 1:]:
                            if not right.get("active", 1):
                                continue
                            a = as_text(left.get("content"))
                            b = as_text(right.get("content"))
                            if not a or not b:
                                continue
                            similarity = SequenceMatcher(None, a, b).ratio() if SequenceMatcher else 0
                            if similarity < 0.90:
                                continue
                            keeper = left
                            drop = right
                            importance = max(int(keeper.get("importance") or 0), int(drop.get("importance") or 0))
                            pinned = max(int(keeper.get("pinned") or 0), int(drop.get("pinned") or 0))
                            content = a if len(a) >= len(b) else b
                            expires = None if pinned or importance >= 80 else (self._now() + (180 if importance >= 40 else 7) * 86400000)
                            conn.execute("UPDATE memories SET content=?, importance=?, pinned=?, expires_at=?, updated_at=?, active=1 WHERE id=?",
                                         (content, importance, pinned, expires, self._now(), keeper["id"]))
                            conn.execute("UPDATE memories SET active=0 WHERE id=?", (drop["id"],))
                            drop["active"] = 0
                            merged += 1
        finally:
            conn.close()
        adjusted = 0
        conn = self._connect()
        try:
            with conn:
                for row in rows:
                    if not row.get("active", 1) or int(row.get("pinned") or 0):
                        continue
                    memory_id = int(row.get("id") or 0)
                    importance = int(row.get("importance") or 0)
                    last_access = int(row.get("last_access_at") or 0)
                    access_count = int(row.get("access_count") or 0)
                    new_importance = importance
                    expires = row.get("expires_at")
                    if last_access:
                        age_days = max(0.0, (now - last_access) / 86400000.0)
                        if age_days <= 14 and access_count >= 5 and importance < 95:
                            new_importance = min(95, importance + 3)
                            if new_importance >= 80:
                                expires = None
                        elif age_days > 120 and importance > 40:
                            new_importance = max(30, importance - 5)
                            if new_importance < 40:
                                expires = now + 7 * 86400000
                    if new_importance != importance or expires != row.get("expires_at"):
                        conn.execute("UPDATE memories SET importance=?, expires_at=? WHERE id=?",
                                     (new_importance, expires, memory_id))
                        adjusted += 1
        finally:
            conn.close()
        self.service.log("记忆整理完成: 合并=%d 调整=%d" % (merged, adjusted))
        return {"merged": merged, "adjusted": adjusted}

    def _maybe_consolidate(self):
        cfg = self._cfg()
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(first_non_blank(self.service.config_store.get().get("timeZone"), "Asia/Shanghai")))
        except Exception:
            now = datetime.now()
        hour = clamp_int(cfg.get("consolidateHour"), 0, 23, 4)
        if now.hour < hour:
            return
        day = now.strftime("%Y-%m-%d")
        if self._meta_get("last_consolidate_day") == day:
            return
        self._meta_set("last_consolidate_day", day)
        try:
            self.consolidate()
        except Exception as error:
            self.service.log("记忆整理失败: %s" % error)


    def _extract(self, item):
        config = self.service.config_store.get()
        cfg = config.get("memory") if isinstance(config.get("memory"), dict) else {}
        if not cfg.get("enabled", True):
            return
        chat_key = as_text(item.get("chat_key"))
        scene = as_text(item.get("scene"))
        target = as_text(item.get("target"))
        user_id = as_text(item.get("user_id"))
        history = []
        engine = getattr(self.service, "auto_engine", None)
        if engine is not None:
            try:
                history = engine._history_messages(chat_key, 30)
            except Exception:
                history = []
        lines = []
        for row in history:
            role = as_text(row.get("role"))
            content = as_text(row.get("content"))
            if content:
                lines.append(("%s：%s" % ("用户" if role == "user" else "她", content))[:400])
        if not lines:
            lines.append("用户：" + as_text(item.get("text")))
        system_prompt = ('你是记忆整理器。从最近对话提取值得长期保存的信息，只输出 JSON 数组，不要解释。'
                         '每条格式：content 字符串、kind 为 fact/event/preference/promise/relationship、'
                         'scope 为 user/group、subject_qq 为 QQ号或空、importance 0-100、confidence 0-100。'
                         '规则：只写对话里明确出现的事实，不编造；日常寒暄和哈哈哈不记；最多 5 条；'
                         '关于某个人的长期事实用 user，群内事件或约定用 group；不确定就不写。')
        user_content = "[最近对话]\n" + "\n".join(lines[-30:])
        result = call_deepseek(config, [{"role": "system", "content": system_prompt},
                                        {"role": "user", "content": user_content}], effort_override="low")
        if not result.get("ok"):
            return
        reply = as_text(result.get("reply")).strip()
        fence = chr(96) + chr(96) + chr(96)
        reply = reply.replace(fence + "json", "").replace(fence, "").strip()
        match = re.search(r"\[[\s\S]*\]", reply)
        if not match:
            return
        try:
            candidates = json.loads(match.group(0))
        except Exception:
            return
        if not isinstance(candidates, list):
            return
        group_id = chat_key.split(":", 1)[1] if scene == "group" and chat_key.startswith("group:") else ""
        for candidate in candidates[:5]:
            if not isinstance(candidate, dict):
                continue
            content = as_text(candidate.get("content")).strip()
            if len(content) < 4 or len(content) > 300:
                continue
            kind = as_text(candidate.get("kind")) or "fact"
            if kind not in ("fact", "event", "preference", "promise", "relationship"):
                kind = "fact"
            scope = as_text(candidate.get("scope")).lower()
            if scope not in ("user", "group"):
                scope = "user"
            subject = as_text(candidate.get("subject_qq")).strip()
            confidence = clamp_int(candidate.get("confidence"), 0, 100, 70)
            if confidence < 40:
                continue
            importance = clamp_int(candidate.get("importance"), 0, 100, 45)
            personal = ("生日", "考试", "工作", "宠物", "喜欢", "讨厌", "答应", "承诺", "秘密", "家人", "学校", "住")
            if any(word in content for word in personal):
                importance = min(100, importance + 8)
            if scope == "group" and group_id:
                self.add("group", group_id, subject, content, kind, importance, confidence, False, "auto")
            else:
                if not subject:
                    subject = user_id
                self.add("user", subject, subject, content, kind, importance, confidence, False, "auto")
        self.service.log("记忆抽取完成: chat=%s 候选=%d" % (chat_key, len(candidates)))




class ASRManager:
    def __init__(self, service):
        self.service = service

    def _cfg(self):
        config = self.service.config_store.get()
        return config.get("asr") if isinstance(config.get("asr"), dict) else {}

    def _ffmpeg(self):
        return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

    def _download(self, source):
        source = as_text(source).strip()
        if not source:
            return b""
        if source.startswith("base64://"):
            return base64.b64decode(source[len("base64://"):])
        if source.startswith("/app/"):
            host = "/opt/snowluma" + source[len("/app"):]
            if os.path.isfile(host):
                return open(host, "rb").read(20 * 1024 * 1024)
        if os.path.isfile(source):
            return open(source, "rb").read(20 * 1024 * 1024)
        request = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0 SnowLumaBridge"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(20 * 1024 * 1024)

    def transcribe_event(self, event, config):
        cfg = self._cfg()
        if not cfg.get("enabled", True) or not as_text(cfg.get("apiKey")):
            return ""
        try:
            voice = skills_ext.extract_voice(event)
        except Exception:
            voice = None
        if not voice:
            return ""
        source = voice.get("url") or voice.get("file") or voice.get("path") or ""
        if not source:
            return ""
        try:
            raw = self._download(source)
        except Exception as error:
            self.service.log("语音下载失败: %s" % error)
            return ""
        if not raw:
            return ""
        if len(raw) > 12 * 1024 * 1024:
            self.service.log("语音文件过大，跳过转写")
            return ""
        temp_dir = "/opt/snowluma/data/voice-cache"
        try:
            os.makedirs(temp_dir, exist_ok=True)
        except Exception:
            temp_dir = "/tmp"
        stamp = str(int(time.time() * 1000))
        silk_path = os.path.join(temp_dir, "voice_%s.silk" % stamp)
        pcm_path = os.path.join(temp_dir, "voice_%s.pcm" % stamp)
        pcm16_path = os.path.join(temp_dir, "voice_%s_16.pcm" % stamp)
        try:
            with open(silk_path, "wb") as handle:
                handle.write(raw)
            decoded_ok = False
            if b"#!SILK_" in raw[:20]:
                decoder = as_text(cfg.get("silkDecoder")) or "/opt/snowluma/bin/silk_decoder"
                if os.path.isfile(decoder):
                    result = subprocess.run([decoder, silk_path, pcm_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                    decoded_ok = result.returncode == 0 and os.path.isfile(pcm_path) and os.path.getsize(pcm_path) > 0
                if decoded_ok:
                    subprocess.run([self._ffmpeg(), "-y", "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1",
                                    "-i", pcm_path, "-f", "s16le", "-ar", "16000", "-ac", "1", pcm16_path],
                                   timeout=30, check=False)
            if not decoded_ok or not os.path.isfile(pcm16_path) or os.path.getsize(pcm16_path) == 0:
                subprocess.run([self._ffmpeg(), "-y", "-loglevel", "error", "-i", silk_path,
                                "-f", "s16le", "-ar", "16000", "-ac", "1", pcm16_path], timeout=30, check=False)
            if not os.path.isfile(pcm16_path) or os.path.getsize(pcm16_path) == 0:
                self.service.log("语音解码失败，跳过转写")
                return ""
            pcm = open(pcm16_path, "rb").read()
            max_seconds = clamp_int(cfg.get("maxSeconds"), 1, 300, 60)
            if len(pcm) > max_seconds * 32000:
                pcm = pcm[:max_seconds * 32000]
            return self._recognize(pcm, cfg)
        except Exception as error:
            self.service.log("语音转写失败: %s: %s" % (error.__class__.__name__, error))
            return ""
        finally:
            for path in (silk_path, pcm_path, pcm16_path):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass

    def _recognize(self, pcm, cfg):
        endpoint = as_text(cfg.get("endpoint")) or "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        api_key = as_text(cfg.get("apiKey"))
        model = as_text(cfg.get("model")) or "paraformer-realtime-v2"
        client = SimpleWebSocketClient(endpoint, timeout=0.5, extra_headers={"Authorization": "Bearer " + api_key})
        try:
            client.connect()
        except Exception as error:
            self.service.log("ASR 连接失败: %s: %s" % (error.__class__.__name__, error))
            return ""
        task_id = str(uuid.uuid4())
        run = {"header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
               "payload": {"task_group": "audio", "task": "asr", "function": "recognition", "model": model,
                           "parameters": {"format": "pcm", "sample_rate": 16000, "language_hints": ["zh"],
                                          "punctuation_prediction_enabled": True, "semantic_punctuation_enabled": True},
                           "input": {}}}
        texts = []
        try:
            client.send_text(json.dumps(run, ensure_ascii=False))
            deadline = time.time() + 12
            started = False
            while time.time() < deadline:
                message = client.read_message()
                if not message:
                    continue
                if message.get("opcode") != 1:
                    continue
                data = json.loads(message.get("payload", b"{}").decode("utf-8", "replace"))
                event = (data.get("header") or {}).get("event", "")
                if event == "task-started":
                    started = True
                    break
                if event == "task-failed":
                    return ""
            if not started:
                return ""
            for offset in range(0, len(pcm), 3200):
                client._send_frame(0x2, pcm[offset:offset + 3200])
            client.send_text(json.dumps({"header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                                         "payload": {"input": {}}}, ensure_ascii=False))
            deadline = time.time() + 10
            while time.time() < deadline:
                message = client.read_message()
                if not message or message.get("opcode") != 1:
                    continue
                data = json.loads(message.get("payload", b"{}").decode("utf-8", "replace"))
                event = (data.get("header") or {}).get("event", "")
                if event == "result-generated":
                    sentence = (data.get("payload") or {}).get("output", {}).get("sentence") or {}
                    if sentence.get("text"):
                        texts.append((bool(sentence.get("sentence_end")), as_text(sentence.get("text"))))
                if event in ("task-finished", "task-failed"):
                    break
        except Exception as error:
            self.service.log("ASR 识别失败: %s: %s" % (error.__class__.__name__, error))
            return ""
        finally:
            try:
                client.close()
            except Exception:
                pass
        final = [text for ended, text in texts if ended]
        if final:
            return final[-1]
        return texts[-1][1] if texts else ""




class AnomalyMonitor:
    """Cheap, mechanical anomaly detector for outgoing messages and errors."""

    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.sends = {}
        self.errors = []
        self.last_alert = {}
        self.blocked_until = {}

    def _cfg(self):
        config = self.service.config_store.get()
        return config.get("safety") if isinstance(config.get("safety"), dict) else {}

    def _alert(self, kind, detail):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return
        now = int(time.time() * 1000)
        cooldown = max(60, int(cfg.get("alertCooldownSeconds", 600) or 600)) * 1000
        with self.lock:
            if now - int(self.last_alert.get(kind) or 0) < cooldown:
                return
            self.last_alert[kind] = now
        owner = ""
        try:
            perms = self.service.config_store.get().get("permissions") or {}
            owner = as_text((perms.get("ownerUsers") or [""])[0])
        except Exception:
            owner = ""
        if not owner:
            return
        try:
            self.service.log("异常告警: %s %s" % (kind, detail))
            self.service.send_message_now("private", owner, "【异常告警】\n" + as_text(detail)[:1200],
                                          skip_monitor=True)
        except Exception as error:
            try:
                self.service.log("异常告警发送失败: %s" % error)
            except Exception:
                pass

    def check_send(self, scene, target, text):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return True
        chat_key = "%s:%s" % (scene, target)
        now = int(time.time() * 1000)
        text = as_text(text)
        with self.lock:
            if now < int(self.blocked_until.get(chat_key) or 0):
                return False
            duplicate_window = max(30, safe_int(cfg.get("duplicateWindowSeconds"), 120)) * 1000
            rows = self.sends.setdefault(chat_key, [])
            rows[:] = [item for item in rows if now - item[0] < 600000]
            same = [item for item in rows if item[1] == text and now - item[0] < duplicate_window]
            recent = [item for item in rows if now - item[0] < 60000]
            duplicate_limit = max(3, safe_int(cfg.get("duplicateLimit"), 6))
            burst_limit = max(5, safe_int(cfg.get("messagesPerMinuteLimit"), 20))
            reason = ""
            block_seconds = 300
            if text and len(same) + 1 >= duplicate_limit:
                reason = "同一会话 %d 秒内重复同一句 %d 次" % (duplicate_window // 1000, len(same) + 1)
                block_seconds = max(10, safe_int(cfg.get("duplicateBlockSeconds"), 60))
            elif len(recent) + 1 >= burst_limit:
                reason = "同一会话 60 秒内发送 %d 条" % (len(recent) + 1)
                block_seconds = max(10, safe_int(cfg.get("burstBlockSeconds"), 300))
            if reason:
                self.blocked_until[chat_key] = now + block_seconds * 1000
                self._alert("outgoing:%s" % chat_key,
                            "会话 %s：%s；已暂停该会话发送 %d 秒" % (chat_key, reason, block_seconds))
                return False
            rows.append((now, text))
            return True

    def record_error(self, source, message):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return
        now = int(time.time() * 1000)
        with self.lock:
            self.errors[:] = [item for item in self.errors if now - item[0] < 300000]
            self.errors.append((now, as_text(source), as_text(message)[:200]))
            count = len(self.errors)
        if count >= max(3, safe_int(cfg.get("errorBurstLimit"), 5)):
            self._alert("errors", "5 分钟内连续错误 %d 次，最近：%s: %s" % (
                count, as_text(source), as_text(message)[:200]))

    def summary(self):
        now = int(time.time() * 1000)
        with self.lock:
            errors = [item for item in self.errors if now - item[0] < 3600000]
            blocked = sum(1 for until in self.blocked_until.values() if int(until or 0) > now)
        if not errors and not blocked:
            return ""
        lines = []
        if errors:
            lines.append("最近一小时错误 %d 次；最近：%s" % (len(errors), errors[-1][2][:160]))
        if blocked:
            lines.append("当前被异常监控暂停的会话：%d 个" % blocked)
        return "\n".join(lines)


class DailyDigestManager:
    """Send one owner-only daily summary through QQ private chat."""

    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = None
        self.catch_up_done = False

    def _cfg(self):
        config = self.service.config_store.get()
        behavior = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
        return behavior.get("dailyDigest") if isinstance(behavior.get("dailyDigest"), dict) else {}

    def _now(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(first_non_blank(self.service.config_store.get().get("timeZone"), "Asia/Shanghai")))
        except Exception:
            return datetime.now()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="snowluma-daily-digest", daemon=True)
        self.thread.start()

    def stop(self, timeout=2.0):
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.1, timeout))
        self.thread = None

    def _catch_up_missing(self):
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return
        try:
            from datetime import timedelta
            now = self._now()
            today = now.strftime("%Y-%m-%d")
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            with self.service.state_store.lock:
                last = as_text(self.service.state_store.data.get("dailyDigestLastDate"))
            if last and last >= yesterday:
                return
            date_key = yesterday
            text = skills_ext.build_daily_digest(self.service, self.service.config_store.get(), date_key)
            empty = (not text.strip()) or ("今天没有新的记忆或关系变化" in text)
            if not text.strip():
                text = "【小鲸鱼今日小结 · %s】" % date_key + chr(10) + "今天没有新的记忆或关系变化。"
            if empty and not cfg.get("sendEmpty", True):
                with self.service.state_store.lock:
                    self.service.state_store.data["dailyDigestLastDate"] = date_key
                    self.service.state_store.dirty = True
                return
            owners = [as_text(item) for item in (((self.service.config_store.get().get("permissions") or {}).get("ownerUsers") or []))]
            if not owners:
                return
            result = self.service.send_message_now(
                "private", owners[0], ("【补发 · %s】" % date_key + chr(10) + text)[:1800])
            if result.get("ok"):
                with self.service.state_store.lock:
                    self.service.state_store.data["dailyDigestLastDate"] = date_key
                    self.service.state_store.dirty = True
                self.service.log("每日小结补发已发送: %s" % date_key)
            else:
                self.service.log("每日小结补发失败: %s" % result.get("error"))
        except Exception as error:
            try:
                self.service.log("每日小结补发异常: %s: %s" % (error.__class__.__name__, error))
            except Exception:
                pass

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                cfg = self._cfg()
                if not cfg.get("enabled", True):
                    time.sleep(60)
                    continue
                if not self.catch_up_done:
                    self.catch_up_done = True
                    self._catch_up_missing()
                now = self._now()
                target = first_non_blank(cfg.get("time"), "23:00")
                try:
                    hour, minute = [int(part) for part in target.split(":")[:2]]
                except Exception:
                    hour, minute = 23, 0
                if now.hour * 60 + now.minute < hour * 60 + minute:
                    time.sleep(30)
                    continue
                date_key = now.strftime("%Y-%m-%d")
                with self.service.state_store.lock:
                    last = as_text(self.service.state_store.data.get("dailyDigestLastDate"))
                if last == date_key:
                    time.sleep(60)
                    continue
                text = skills_ext.build_daily_digest(self.service, self.service.config_store.get(), date_key)
                empty = (not text.strip()) or ("今天没有新的记忆或关系变化" in text)
                if not text.strip():
                    text = "【小鲸鱼今日小结 · %s】\n今天没有新的记忆或关系变化。" % date_key
                if empty and not cfg.get("sendEmpty", True):
                    with self.service.state_store.lock:
                        self.service.state_store.data["dailyDigestLastDate"] = date_key
                        self.service.state_store.dirty = True
                    time.sleep(60)
                    continue
                owners = [as_text(item) for item in (((self.service.config_store.get().get("permissions") or {}).get("ownerUsers") or []))]
                if not owners:
                    time.sleep(60)
                    continue
                result = self.service.send_message_now("private", owners[0], text[:1800])
                if result.get("ok"):
                    with self.service.state_store.lock:
                        self.service.state_store.data["dailyDigestLastDate"] = date_key
                        self.service.state_store.dirty = True
                    self.service.log("每日小结已发送: %s" % date_key)
                else:
                    self.service.log("每日小结发送失败: %s" % result.get("error"))
            except Exception as error:
                try:
                    self.service.log("每日小结异常: %s: %s" % (error.__class__.__name__, error))
                except Exception:
                    pass
            time.sleep(30)


class SelfReviewManager:
    """One low-cost owner-only self-review per day."""

    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = None
        self.running = False
        self.lock = threading.RLock()

    def _cfg(self):
        config = self.service.config_store.get()
        behavior = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
        return behavior.get("selfReview") if isinstance(behavior.get("selfReview"), dict) else {}

    def _now(self):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(first_non_blank(self.service.config_store.get().get("timeZone"), "Asia/Shanghai")))
        except Exception:
            return datetime.now()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="snowluma-self-review", daemon=True)
        self.thread.start()

    def stop(self, timeout=2.0):
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(max(0.1, timeout))
        self.thread = None

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                cfg = self._cfg()
                if not cfg.get("enabled", True):
                    time.sleep(60)
                    continue
                now = self._now()
                target = first_non_blank(cfg.get("time"), "22:30")
                try:
                    hour, minute = [int(part) for part in target.split(":")[:2]]
                except Exception:
                    hour, minute = 22, 30
                if now.hour * 60 + now.minute < hour * 60 + minute:
                    time.sleep(30)
                    continue
                date_key = now.strftime("%Y-%m-%d")
                with self.service.state_store.lock:
                    last = as_text(self.service.state_store.data.get("selfReviewLastDate"))
                if last == date_key:
                    time.sleep(60)
                    continue
                with self.service.state_store.lock:
                    self.service.state_store.data["selfReviewLastDate"] = date_key
                    self.service.state_store.dirty = True
                self._run(date_key)
            except Exception as error:
                try:
                    self.service.log("每日复盘异常: %s: %s" % (error.__class__.__name__, error))
                except Exception:
                    pass
            time.sleep(60)

    def run_now(self):
        if self.running:
            return False
        date_key = self._now().strftime("%Y-%m-%d")
        threading.Thread(target=self._run, args=(date_key,), name="snowluma-self-review-now", daemon=True).start()
        return True

    def _material(self, date_key):
        config = self.service.config_store.get()
        parts = [skills_ext.build_daily_digest(self.service, config, date_key)]
        rows = []
        try:
            with self.service.state_store.lock:
                keys = list((self.service.state_store.data.get("history") or {}).keys())
            for key in keys[-3:]:
                messages = self.service.auto_engine._history_messages(key, 8)
                if not messages:
                    continue
                rows.append("【会话 %s】" % key)
                for item in messages[-8:]:
                    role = as_text(item.get("role"))
                    content = as_text(item.get("content"))[:180].replace("\n", " ")
                    if content:
                        rows.append("- %s：%s" % (role, content))
        except Exception:
            pass
        if rows:
            parts.append("最近对话片段：\n" + "\n".join(rows[-30:]))
        try:
            monitor_summary = self.service.monitor.summary() if hasattr(self.service, "monitor") else ""
        except Exception:
            monitor_summary = ""
        if monitor_summary:
            parts.append("异常监控：\n" + monitor_summary)
        try:
            with self.service.state_store.lock:
                pending_rules = [dict(item) for item in (self.service.state_store.data.get("ruleCandidates") or []) if isinstance(item, dict)]
                approved_rules = [dict(item) for item in (self.service.state_store.data.get("approvedRules") or []) if isinstance(item, dict)]
            rule_lines = []
            for item in pending_rules[-5:]:
                proposal = as_text(item.get("proposal")).strip()
                if proposal:
                    rule_lines.append("- 待确认：" + proposal[:120])
            for item in approved_rules[-10:]:
                proposal = as_text(item.get("proposal")).strip()
                if proposal:
                    rule_lines.append("- 已通过：" + proposal[:120])
            if rule_lines:
                parts.append("已有规则（同一件事不要重复提）：\n" + "\n".join(rule_lines)[:2500])
        except Exception:
            pass
        return "\n\n".join(part for part in parts if part.strip())[:6000]

    def _run(self, date_key=""):
        with self.lock:
            if self.running:
                return
            self.running = True
        try:
            config = self.service.config_store.get()
            date_key = date_key or self._now().strftime("%Y-%m-%d")
            material = self._material(date_key)
            if not material.strip():
                self.service.log("每日复盘跳过：没有材料")
                return
            try:
                qq_state = self.service.state_store.snapshot().get("qq") or {}
            except Exception:
                qq_state = {}
            bot_qq = as_text(config.get("botQQ")) or as_text(qq_state.get("selfId")) or "机器人"
            bot_names = []
            try:
                group_reply = ((config.get("skills") or {}).get("groupReply") or {})
                bot_names = [as_text(item) for item in (group_reply.get("botNames") or []) if as_text(item)]
            except Exception:
                bot_names = []
            bot_label = "、".join(bot_names[:6]) or "小鲸鱼"
            system = (
                "你是小鲸鱼的每日自我复盘器。你是小鲸鱼（QQ %s；群昵称/别名：%s），对话里的 [你] 指你自己，不是主人或群友。" % (bot_qq, bot_label) +
                "只输出 JSON，不要解释。"
                "看今天的记忆/关系摘要和最近对话，找出反复出现、值得长期改进的社交或表达问题。"
                "不要凭一句话过度总结，不要评价用户隐私。输出："
                '{"summary":"今天的一句话总结","issues":["最多3个具体观察"],'
                '"rule_candidates":[{"title":"短标题","proposal":"以后应该怎么做","reason":"依据"}],'
                '"memory_candidates":[{"content":"值得记住的事实","kind":"fact|preference|event","scope":"user|group","importance":50}]}'
                "规则候选最多1条；如果已有规则或待确认候选已经覆盖同一件事，不要再提；没有明确证据就给空数组；不要为了填字段硬编。"
                "写 rule_candidate 前先确认主语和宾语：是“别人怎么叫你/怎么对你”，还是“你怎么叫别人/怎么对别人”；不要把别人给你的称呼写成你去称呼别人。"
                "输出前自查：谁在说、谁被称呼、你要改变的是哪一方；方向拿不准就不提，不要猜。"
                "rule_candidate 的 proposal 尽量用第一人称写清：当谁对我做什么时，我该怎么做；不要写成“以后称呼谁为……”。"
            )
            result = call_deepseek(config, [{"role": "system", "content": system},
                                            {"role": "user", "content": material}], effort_override="low")
            if not result.get("ok"):
                self.service.log("每日复盘模型失败: %s" % result.get("error"))
                return
            raw = as_text(result.get("reply")).strip()
            import re as _re
            match = _re.search(r"\{[\s\S]*\}", raw)
            data = {}
            if match:
                try:
                    import json as _json
                    data = _json.loads(match.group(0))
                except Exception:
                    data = {}
            summary = as_text(data.get("summary") if isinstance(data, dict) else "") or raw[:300]
            issues = data.get("issues") if isinstance(data, dict) and isinstance(data.get("issues"), list) else []
            rules = data.get("rule_candidates") if isinstance(data, dict) and isinstance(data.get("rule_candidates"), list) else []
            memories = data.get("memory_candidates") if isinstance(data, dict) and isinstance(data.get("memory_candidates"), list) else []
            for candidate in rules[:1]:
                if not isinstance(candidate, dict):
                    continue
                proposal = as_text(candidate.get("proposal")).strip()
                if len(proposal) >= 6:
                    skills_ext.propose_rule(self.service, config,
                                            as_text(candidate.get("title")) or proposal[:40],
                                            proposal, as_text(candidate.get("reason")),
                                            scope="global", source="self_review")
            try:
                with self.service.state_store.lock:
                    history_keys = list((self.service.state_store.data.get("history") or {}).keys())
                group_key = next((key for key in reversed(history_keys) if as_text(key).startswith("group:")), "")
                owner_qq = as_text((((config.get("permissions") or {}).get("ownerUsers") or [""])[0]))
                for candidate in memories[:3]:
                    if not isinstance(candidate, dict):
                        continue
                    content = as_text(candidate.get("content")).strip()
                    if len(content) < 4 or len(content) > 300:
                        continue
                    kind = as_text(candidate.get("kind")) or "fact"
                    importance = max(0, min(100, int(candidate.get("importance") or 45)))
                    if group_key:
                        self.service.memories.add("group", group_key.split(":", 1)[-1], "", content, kind, importance, 75, False, "self_review")
                    elif owner_qq:
                        self.service.memories.add("user", owner_qq, owner_qq, content, kind, importance, 75, False, "self_review")
            except Exception:
                pass
            lines = ["【每日自复盘 · %s】" % date_key, "总结：" + summary[:500]]
            if issues:
                lines.append("观察：")
                lines.extend("- " + as_text(item)[:180] for item in issues[:3])
            if rules:
                lines.append("规则候选：已私聊单独发送，回复 /规则候选 查看")
            if memories:
                lines.append("新记忆：%d 条" % len(memories))
            owner = ""
            try:
                owner = as_text((((config.get("permissions") or {}).get("ownerUsers") or [""])[0]))
            except Exception:
                owner = ""
            if owner:
                self.service.send_message_now("private", owner, "\n".join(lines)[:1800], skip_monitor=True)
            with self.service.state_store.lock:
                self.service.state_store.data["selfReviewLastDate"] = date_key
                self.service.state_store.dirty = True
        except Exception as error:
            self.service.log("每日复盘失败: %s: %s" % (error.__class__.__name__, error))
        finally:
            with self.lock:
                self.running = False


class SnowLumaService:
    """控制 HTTP + OneBot WS + DeepSeek 自动回复 的组合服务。"""

    def __init__(self, args):
        self.args = args
        self.state_dir = os.path.abspath(as_text(args.state_dir).strip() or ".")
        ensure_dir(self.state_dir)
        self.log_file = os.path.abspath(first_non_blank(args.log_file) or os.path.join(self.state_dir, "bridge.log"))
        self.logger = BridgeLogger(self.log_file)
        self.logger.redact_provider = self._collect_secrets
        self.config_file = os.path.join(self.state_dir, "config.json")
        self.state_file = os.path.join(self.state_dir, "state.json")
        self.pid_file = os.path.join(self.state_dir, "bridge.pid")
        self.control_port = clamp_int(getattr(args, "port", DEFAULT_CONTROL_PORT), 1, 65535, DEFAULT_CONTROL_PORT)
        self.started_at = now_ms()
        self.shutdown_event = threading.Event()
        self.shutdown_reason = ""
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self.config_store = ConfigStore(self.config_file, self.logger)
        self.state_store = StateStore(self.state_file, self.logger)
        config = self.config_store.get()
        launch_token = first_non_blank(getattr(args, "token", ""))
        self.control_token = first_non_blank(launch_token, config.get("bridgeToken"))
        if launch_token and config.get("bridgeToken") != launch_token:
            # 仅内存回填：config.json 由 JS 侧唯一写盘
            try:
                self.config_store.save_patch({"bridgeToken": launch_token})
            except ValueError:
                pass
        self.udocker = UDockerManager(self, binary_override=getattr(args, "udocker_binary", ""))
        self.bridge = OneBotBridge(self)
        self.auto_engine = AutoReplyEngine(self)
        self.proactive = ProactiveManager(self)
        self.profiles = ProfileManager(self)
        self.mood = MoodManager(self)
        self.voice = VoiceService(self)
        self.sleep = SleepManager(self)
        self.memories = MemoryManager(self)
        self.relationships = snowluma_relationships.RelationshipStore(self)
        self.conversation = snowluma_conversation.ConversationStore(self)
        self.reflections = snowluma_reflection.ReflectionEngine(self)
        self.asr = ASRManager(self)
        self.memewiki = snowluma_memewiki.MemeWiki(self)
        self.monitor = AnomalyMonitor(self)
        self.self_review = SelfReviewManager(self)
        self.daily_digest = DailyDigestManager(self)
        self.current_video = None
        self.control_server = None
        self._control_thread = None
        self._saver_thread = None
        self._refresher_thread = None
        self._config_watcher_thread = None
        self._deepseek_lock = threading.Lock()
        self._deepseek_state = {"ok": None, "error": "", "at": 0, "elapsedMs": 0, "model": ""}
        self._qq_status_lock = threading.Lock()
        self._qq_last = {"loginInfoAt": 0}

    # ---- 基础 ----

    def log(self, message):
        self.logger.log(message)

    def _collect_secrets(self):
        secrets = []
        try:
            config = self.config_store.get()
            deepseek = config.get("deepseek") if is_object(config.get("deepseek")) else {}
            secrets.extend([
                as_text(deepseek.get("apiKey")),
                as_text(config.get("onebotToken")),
                as_text(config.get("bridgeToken")),
            ])
        except Exception:
            pass
        secrets.append(as_text(getattr(self, "control_token", "")))
        return [item for item in secrets if item]

    def uptime_ms(self):
        return now_ms() - self.started_at

    # ---- PID ----

    def _write_pid_file(self):
        try:
            with open(self.pid_file, "w", encoding="utf-8") as handle:
                handle.write("%d\n" % os.getpid())
            chmod_quiet(self.pid_file, 0o600)
        except OSError as error:
            self.log("写 pid 文件失败: %s: %s" % (error.__class__.__name__, error))

    def _remove_pid_file(self):
        try:
            with open(self.pid_file, "r", encoding="utf-8") as handle:
                content = handle.read().strip()
            if content == str(os.getpid()):
                os.unlink(self.pid_file)
        except OSError:
            pass

    # ---- 生命周期 ----

    def start(self):
        self._write_pid_file()
        try:
            config_for_cleanup = self.config_store.get()
            behavior = config_for_cleanup.get("behavior") if isinstance(config_for_cleanup.get("behavior"), dict) else {}
            context_cfg = behavior.get("context") if isinstance(behavior.get("context"), dict) else {}
            mode = as_text(context_cfg.get("mode")).lower()
            max_age = 24 * 3600
            if mode in ("daily", "persistent"):
                max_age = max(1, safe_int(context_cfg.get("retentionDays"), 30)) * 24 * 3600
            skills_ext.cleanup_cache(self, max_age_seconds=max_age)
        except Exception:
            pass
        self.log("=" * 70)
        self.log("snowluma_service %s 启动 pid=%d python=%s" % (VERSION, os.getpid(), sys.version.split()[0]))
        self.log("state_dir=%s" % self.state_dir)
        self.log("控制面: http://127.0.0.1:%d%s" % (
            self.control_port,
            "  (X-Auth-Token 鉴权已启用)" if self.control_token else "  (警告: 未提供 --token，鉴权关闭)",
        ))
        config = self.config_store.get()
        self._migrate_legacy_history(config)
        self.log("容器: name=%s image=%s 端口 webui=%s onebot=%s" % (
            config.get("containerName"), config.get("image"),
            config.get("webuiPort"), config.get("onebotPort"),
        ))
        if os.path.exists(self.config_file):
            self.log("config.json 已读取（由 JS 侧写盘；本服务只读 + 内存热更新）")
        else:
            self.log("config.json 不存在：使用默认配置，等待 JS/UI 保存后自动热加载")
        self.start_control_server()
        try:
            self.relationships.ensure_schema()
        except Exception as rel_error:
            self.log("关系库初始化失败: %s" % rel_error)
        try:
            self.conversation.ensure_schema()
        except Exception as conv_error:
            self.log("日级对话库初始化失败: %s" % conv_error)
        self.bridge.start()
        self.daily_digest.start()
        self.self_review.start()
        self.auto_engine.start()
        self.proactive.start()
        self.profiles.start()
        self.sleep.start()
        self.memories.start()
        self.reflections.start()
        try:
            self.voice.cleanup()
        except Exception:
            pass
        self._start_background_threads()
        self.log("snowluma bridge 已就绪")

    def _migrate_legacy_history(self, config):
        behavior = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
        if not behavior.get("migrateLegacyHistory", True):
            return
        try:
            with self.state_store.lock:
                data = self.state_store.data
                if data.get("behaviorHistoryV2At"):
                    return
                archive = {"history": data.get("history") or {}, "groupContext": data.get("groupContext") or {},
                           "archivedAt": now_ms()}
                try:
                    with open(os.path.join(self.state_dir, "history.pre-v2.json"), "w", encoding="utf-8") as handle:
                        json.dump(archive, handle, ensure_ascii=False)
                except OSError as error:
                    self.log("旧历史归档失败: %s" % error)
                data["history"] = {}
                data["groupContext"] = {}
                data["behaviorHistoryV2At"] = now_ms()
                self.state_store.dirty = True
            self.log("旧聊天上下文已归档并清空（history.pre-v2.json）")
        except Exception as error:
            self.log("旧上下文迁移失败: %s" % error)

    def start_control_server(self):
        handler = make_control_handler(self)
        try:
            server = BridgeHTTPServer(("127.0.0.1", self.control_port), handler)
        except OSError as error:
            raise RuntimeError("控制面端口 %d 绑定失败（可能已有实例在运行）: %s" % (self.control_port, error))
        server.service = self
        thread = threading.Thread(target=server.serve_forever, name="control_http", daemon=True)
        thread.start()
        self.control_server = server
        self._control_thread = thread
        self.log("控制面已监听 127.0.0.1:%d" % self.control_port)

    def _stop_control_server(self):
        server = self.control_server
        self.control_server = None
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    def _start_background_threads(self):
        self._saver_thread = threading.Thread(target=self._saver_loop, name="state_saver", daemon=True)
        self._saver_thread.start()
        self._refresher_thread = threading.Thread(target=self._status_refresh_loop, name="status_refresher", daemon=True)
        self._refresher_thread.start()
        self._config_watcher_thread = threading.Thread(target=self._config_watch_loop, name="config_watcher", daemon=True)
        self._config_watcher_thread.start()

    def _saver_loop(self):
        while not self.shutdown_event.wait(2.0):
            try:
                self.state_store.save_if_dirty()
            except Exception as error:
                self.log("state 保存失败: %s: %s" % (error.__class__.__name__, error))

    def _status_refresh_loop(self):
        if self.shutdown_event.wait(0.3):
            return
        while not self.shutdown_event.is_set():
            try:
                self.udocker.refresh()
            except Exception as error:
                self.log("udocker 状态刷新失败: %s: %s" % (error.__class__.__name__, error))
            if self.shutdown_event.wait(STATUS_REFRESH_INTERVAL_MS / 1000.0):
                return

    def _config_watch_loop(self):
        while not self.shutdown_event.wait(2.0):
            try:
                changed, old, new = self.config_store.reload_if_changed()
            except Exception as error:
                self.log("config.json 热加载失败: %s: %s" % (error.__class__.__name__, error))
                continue
            if not changed:
                continue
            self.log("检测到 config.json 变更，已热加载（仅内存）")
            self._apply_config_changes(old or {}, new or {})

    def _apply_config_changes(self, old, new):
        old = old if is_object(old) else {}
        new = new if is_object(new) else {}
        if old.get("onebotPort") != new.get("onebotPort") or old.get("onebotToken") != new.get("onebotToken"):
            self.log("OneBot 端口/Token 变更，重启 OneBot WebSocket 连接")
            self.bridge.request_restart()

    def run_forever(self):
        while not self.shutdown_event.wait(0.5):
            pass

    def request_shutdown(self, reason):
        with self._shutdown_lock:
            if self.shutdown_event.is_set():
                return
            self.shutdown_reason = first_non_blank(reason, "shutdown")
            self.log("收到退出请求: %s" % self.shutdown_reason)
            self.shutdown_event.set()

    def shutdown(self):
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self.log("开始关闭桥接服务 ...")
        try:
            self.bridge.stop(3.0)
        except Exception as error:
            self.log("停止 OneBot 桥失败: %s" % error)
        try:
            self.auto_engine.stop(2.0)
        except Exception as error:
            self.log("停止自动回复引擎失败: %s" % error)
        try:
            self.state_store.save_if_dirty(force=True)
        except Exception as error:
            self.log("保存 state 失败: %s" % error)
        self._stop_control_server()
        self._remove_pid_file()
        self.log("桥接服务已退出 pid=%d 运行时长=%ds reason=%s" % (
            os.getpid(), max(0, self.uptime_ms() // 1000), self.shutdown_reason or "shutdown"))

    # ---- 状态聚合 ----

    def _qq_status(self):
        state = self.state_store.snapshot()
        qq = state.get("qq") if is_object(state.get("qq")) else {}
        bridge = self.bridge.status()
        self_id = first_non_blank(qq.get("selfId"), bridge.get("selfId"))
        with self._qq_status_lock:
            login_info_at = self._qq_last.get("loginInfoAt", 0)
        return {
            "loggedIn": bool(bridge.get("wsConnected")) and bool(self_id),
            "online": bool(qq.get("online", True)) if bridge.get("wsConnected") else False,
            "account": self_id,
            "selfId": self_id,
            "nickname": first_non_blank(qq.get("nickname")),
            "lastHeartbeatAt": safe_int(qq.get("lastHeartbeatAt"), 0),
            "lastMessageAt": safe_int(qq.get("lastEventAt"), 0),
            "loginInfoAt": safe_int(login_info_at, 0),
        }

    def _deepseek_status(self):
        config = self.config_store.get()
        deepseek = config.get("deepseek") if is_object(config.get("deepseek")) else {}
        with self._deepseek_lock:
            runtime = dict(self._deepseek_state)
        return {
            "configured": bool(as_text(deepseek.get("apiKey")).strip()),
            "model": first_non_blank(deepseek.get("model"), "deepseek-flash"),
            "baseUrl": first_non_blank(deepseek.get("baseUrl")),
            "hasApiKey": bool(as_text(deepseek.get("apiKey")).strip()),
            "lastOk": runtime.get("ok"),
            "lastError": first_non_blank(runtime.get("error")),
            "lastCallAt": safe_int(runtime.get("at"), 0),
            "lastLatencyMs": safe_int(runtime.get("elapsedMs"), 0),
            "lastModel": first_non_blank(runtime.get("model")),
        }

    def build_auto_status(self):
        config = self.config_store.get()
        auto = config.get("autoReply") if is_object(config.get("autoReply")) else {}
        state = self.state_store.snapshot()
        counters = state.get("counters") if is_object(state.get("counters")) else {}
        auto_state = state.get("autoReply") if is_object(state.get("autoReply")) else {}
        return {
            "enabled": bool(auto.get("enabled")),
            "privateEnabled": bool(auto.get("privateEnabled")),
            "groupEnabled": bool(auto.get("groupEnabled")),
            "requireAtInGroup": bool(auto.get("requireAtInGroup")),
            "triggerPrefix": as_text(auto.get("triggerPrefix")),
            "cooldownMs": safe_int(auto.get("cooldownMs"), 0),
            "maxHistory": safe_int(auto.get("maxHistory"), 0),
            "processed": safe_int(counters.get("processed"), 0),
            "skipped": safe_int(counters.get("skipped"), 0),
            "replied": safe_int(counters.get("replied"), 0),
            "errors": safe_int(counters.get("errors"), 0),
            "queued": self.auto_engine.pending_count(),
            "lastError": first_non_blank(auto_state.get("lastError")),
            "lastSkipReason": first_non_blank(auto_state.get("lastSkipReason")),
            "lastProcessedAt": safe_int(auto_state.get("lastProcessedAt"), 0),
            "lastReplyAt": safe_int(auto_state.get("lastReplyAt"), 0),
        }

    def build_health(self):
        return {
            "ok": True,
            "version": VERSION,
            "product": PRODUCT,
            "pid": os.getpid(),
            "uptimeMs": self.uptime_ms(),
            "stateDir": self.state_dir,
        }

    def build_status(self, force=False):
        config = self.config_store.get()
        bridge = self.bridge.status()
        udocker = self.udocker.get_status(force=force)
        container = self.udocker.container_status(force=force)
        state = self.state_store.snapshot()
        counters = state.get("counters") if is_object(state.get("counters")) else {}
        return {
            "ok": True,
            "version": VERSION,
            "product": PRODUCT,
            "pid": os.getpid(),
            "uptimeMs": self.uptime_ms(),
            "startedAt": self.started_at,
            "bridge": {
                "running": True,
                "pid": os.getpid(),
                "uptimeMs": self.uptime_ms(),
                "port": self.control_port,
                "controlPort": self.control_port,
                "onebotPort": clamp_int(config.get("onebotPort"), 1, 65535, 3001),
                "wsConnected": bool(bridge.get("wsConnected")),
                "connectedAt": bridge.get("connectedAt", 0),
                "lastConnectAt": bridge.get("lastConnectAt", 0),
                "lastPacketAt": bridge.get("lastPacketAt", 0),
                "lastHeartbeatAt": bridge.get("lastHeartbeatAt", 0),
                "heartbeatIntervalMs": bridge.get("heartbeatIntervalMs", 0),
                "reconnectCount": bridge.get("reconnectCount", 0),
                "lastError": first_non_blank(bridge.get("lastError")),
                "lastCloseCode": bridge.get("lastCloseCode", 0),
                "lastCloseReason": first_non_blank(bridge.get("lastCloseReason")),
                "url": first_non_blank(bridge.get("url")),
                "path": first_non_blank(bridge.get("path"), "/"),
                "pendingActions": bridge.get("pendingActions", 0),
            },
            "udocker": udocker,
            "container": container,
            "qq": self._qq_status(),
            "autoReply": self.build_auto_status(),
            "deepseek": self._deepseek_status(),
            "whitelist": config.get("whitelist"),
            "config": {
                "containerName": config.get("containerName"),
                "image": config.get("image"),
                "webuiPort": config.get("webuiPort"),
                "onebotPort": config.get("onebotPort"),
                "autoReplyEnabled": bool(config.get("autoReply", {}).get("enabled")),
                "deepseekModel": first_non_blank(config.get("deepseek", {}).get("model")),
            },
            "files": {
                "configFile": self.config_file,
                "stateFile": self.state_file,
                "logFile": self.log_file,
                "pidFile": self.pid_file,
                "udockerConfigFile": self.udocker.overrides_path,
            },
            "controlTokenSet": bool(self.control_token),
            "counters": {
                "processed": safe_int(counters.get("processed"), 0),
                "skipped": safe_int(counters.get("skipped"), 0),
                "replied": safe_int(counters.get("replied"), 0),
                "errors": safe_int(counters.get("errors"), 0),
            },
            "runtime": {
                "python": sys.version.split()[0],
                "platform": sys.platform,
            },
        }

    # ---- 控制面业务 ----

    def _masked_config_payload(self):
        config, flags = self.config_store.masked()
        return {
            "ok": True,
            "config": config,
            "secrets": flags,
            "configFile": self.config_file,
            "readOnly": True,
            "note": "config.json 由 JS 侧写盘；POST /config 仅更新桥接服务内存配置",
        }

    def save_config_patch(self, patch):
        old = self.config_store.get()
        new = self.config_store.save_patch(patch)
        keys = sorted([as_text(key) for key in (patch.keys() if is_object(patch) else [])])
        self.log("配置已热更新（仅内存）: keys=%s" % ",".join(keys))
        self._apply_config_changes(old, new)
        return new

    def set_auto_enabled(self, enabled):
        self.save_config_patch({"autoReply": {"enabled": bool(enabled)}})
        return self.build_auto_status()

    def set_whitelist(self, body):
        patch = {}
        for key in ("mode", "users", "groups"):
            if key in body:
                patch[key] = body[key]
        if not patch:
            raise ValueError("whitelist_patch_empty")
        new = self.save_config_patch({"whitelist": patch})
        config, _flags = self.config_store.masked()
        return {"ok": True, "config": config, "whitelist": new.get("whitelist")}

    def container_action(self, action, lines=200):
        self.udocker.reload_overrides()
        return self.udocker.run_action(action, lines)

    def send_message_now(self, scene, target, text, skip_monitor=False):
        scene = first_non_blank(scene).lower()
        target = first_non_blank(target)
        text = as_text(text)
        if scene not in ("private", "group"):
            return {"ok": False, "error": "scene 必须是 private 或 group", "scene": scene}
        if not target:
            return {"ok": False, "error": "target 不能为空", "scene": scene}
        if not text.strip():
            return {"ok": False, "error": "text 不能为空", "scene": scene}
        if not skip_monitor and hasattr(self, "monitor") and not self.monitor.check_send(scene, target, text):
            self.log("异常监控拦截发送: %s:%s" % (scene, target))
            return {"ok": False, "error": "异常监控拦截", "scene": scene, "target": target}
        if scene == "private":
            action = "send_private_msg"
            params = {"user_id": self._normalize_id(target), "message": text, "auto_escape": True}
        else:
            action = "send_group_msg"
            params = {"group_id": self._normalize_id(target), "message": text, "auto_escape": True}
        try:
            data = self.bridge.call_action(action, params, timeout_ms=ACTION_TIMEOUT_MS)
            return {"ok": True, "scene": scene, "target": target, "action": action, "result": data}
        except OneBotError as error:
            return {"ok": False, "scene": scene, "target": target, "action": action, "error": as_text(error)}
        except Exception as error:
            return {"ok": False, "scene": scene, "target": target, "action": action,
                    "error": "%s: %s" % (error.__class__.__name__, error)}

    @staticmethod
    def _normalize_id(value):
        text = as_text(value).strip()
        if text.lstrip("-").isdigit():
            return int(text)
        return text

    def test_deepseek(self, text):
        config = self.config_store.get()
        system_prompt = as_text(config.get("deepseek", {}).get("systemPrompt"))
        user_text = first_non_blank(text, "你好，请用一句话介绍你自己。")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        result = call_deepseek(config, messages)
        self.note_deepseek_result(result)
        payload = {
            "ok": bool(result.get("ok")),
            "reply": as_text(result.get("reply")),
            "model": as_text(result.get("model")),
            "elapsedMs": safe_int(result.get("elapsedMs"), 0),
        }
        if not result.get("ok"):
            payload["error"] = as_text(result.get("error"))
            self.log("test-deepseek 失败: %s" % payload["error"])
        else:
            self.log("test-deepseek 成功: model=%s 耗时=%sms" % (payload["model"], payload["elapsedMs"]))
        return payload

    def restart_bridge(self):
        self.bridge.request_restart()
        return {"ok": True, "bridge": self.bridge.status()}

    def logs_text(self, lines=200):
        lines = clamp_int(lines, 1, MAX_LOG_TAIL_LINES, 200)
        return tail_text_file(self.log_file, lines)

    def note_deepseek_result(self, result):
        with self._deepseek_lock:
            self._deepseek_state = {
                "ok": bool(result.get("ok")),
                "error": "" if result.get("ok") else as_text(result.get("error")),
                "at": now_ms(),
                "elapsedMs": safe_int(result.get("elapsedMs"), 0),
                "model": as_text(result.get("model")),
            }

    def record_cache_usage(self, chat_key, usage):
        usage = usage if isinstance(usage, dict) else {}
        prompt_tokens = safe_int(usage.get("prompt_tokens", usage.get("input_tokens")), 0)
        hit_tokens = safe_int(usage.get("prompt_cache_hit_tokens"), 0)
        miss_tokens = safe_int(usage.get("prompt_cache_miss_tokens"), 0)
        if not prompt_tokens and not hit_tokens and not miss_tokens:
            return
        chat_key = as_text(chat_key) or "unknown"

        def mutate(data):
            metrics = data.setdefault("cacheMetrics", {})
            if not isinstance(metrics, dict):
                metrics = {}
                data["cacheMetrics"] = metrics
            item = metrics.get(chat_key) if isinstance(metrics.get(chat_key), dict) else {}
            item["requests"] = safe_int(item.get("requests"), 0) + 1
            item["promptTokens"] = safe_int(item.get("promptTokens"), 0) + prompt_tokens
            item["hitTokens"] = safe_int(item.get("hitTokens"), 0) + hit_tokens
            item["missTokens"] = safe_int(item.get("missTokens"), 0) + miss_tokens
            item["lastPromptTokens"] = prompt_tokens
            item["lastAt"] = now_ms()
            metrics[chat_key] = item
        self.state_store.update(mutate)

    # ---- 供 bridge / engine 调用的回调 ----

    def on_bridge_connected(self):
        def mutate(data):
            data["bridge"]["lastConnectAt"] = now_ms()
            data["bridge"]["lastError"] = ""
        self.state_store.update(mutate)

    def on_bridge_reconnect(self, count, error):
        def mutate(data):
            data["bridge"]["reconnectCount"] = safe_int(count, 0)
            data["bridge"]["lastError"] = as_text(error)
        self.state_store.update(mutate)
        self.log("OneBot 重连 #%d lastError=%s" % (safe_int(count, 0), first_non_blank(error, "-")))

    def on_bridge_stopped(self):
        self.log("OneBot 桥已停止")

    def on_qq_self_id(self, self_id, nickname=""):
        self_id = first_non_blank(self_id)
        if not self_id:
            return

        def mutate(data):
            qq = data.setdefault("qq", {})
            qq["selfId"] = self_id
            qq["account"] = self_id
            if nickname:
                qq["nickname"] = nickname
            qq["lastEventAt"] = now_ms()
        self.state_store.update(mutate)

    def on_qq_login_info(self, user_id, nickname=""):
        self.on_qq_self_id(user_id, nickname)
        with self._qq_status_lock:
            self._qq_last["loginInfoAt"] = now_ms()
        self.log("QQ 登录信息: account=%s nickname=%s" % (user_id, nickname or "-"))

    def on_qq_heartbeat(self, self_id, status):
        status = status if is_object(status) else {}
        online = status.get("online")
        if online is None:
            online = status.get("good")

        def mutate(data):
            qq = data.setdefault("qq", {})
            qq["lastHeartbeatAt"] = now_ms()
            if self_id:
                qq["selfId"] = first_non_blank(self_id)
                qq["account"] = first_non_blank(self_id)
            if online is not None:
                qq["online"] = to_bool(online, True)
        self.state_store.update(mutate)

    def on_qq_lifecycle(self, self_id, sub_type=""):
        sub_type = first_non_blank(sub_type)
        if first_non_blank(self_id):
            self.on_qq_self_id(self_id)
        if sub_type:
            self.log("OneBot lifecycle: %s self_id=%s" % (sub_type, first_non_blank(self_id, "-")))
        if sub_type in ("disable", "disconnect"):
            def mutate(data):
                data.setdefault("qq", {})["online"] = False
            self.state_store.update(mutate)

    def on_qq_message_event(self, event):
        def mutate(data):
            data["lastMessageAt"] = now_ms()
            qq = data.setdefault("qq", {})
            qq["lastEventAt"] = now_ms()
        self.state_store.update(mutate)

    def on_onebot_message(self, event):
        self.log("收到 QQ 消息: scene=%s 来自=%s%s msg_id=%s 长度=%d" % (
            first_non_blank(event.get("message_type")),
            first_non_blank(event.get("user_id")),
            (" 群=" + first_non_blank(event.get("group_id"))) if event.get("group_id") else "",
            first_non_blank(event.get("message_id"), "-"),
            len(as_text(event.get("text"))),
        ))
        self.auto_engine.enqueue(event)

    # ---- 容器状态回调 ----

    def record_container_pid(self, pid):
        def mutate(data):
            container = data.setdefault("container", {})
            container["pid"] = safe_int(pid, 0)
            container["startedAt"] = now_ms() if pid else 0
        self.state_store.update(mutate)

    def clear_container_pid(self):
        def mutate(data):
            container = data.setdefault("container", {})
            container["pid"] = 0
            container["startedAt"] = 0
        self.state_store.update(mutate)

    def mark_container_created(self):
        def mutate(data):
            container = data.setdefault("container", {})
            container["createdAt"] = now_ms()
        self.state_store.update(mutate)

    def record_container_action(self, action, result):
        summary = {
            "ok": bool(result.get("ok")),
            "error": first_non_blank(result.get("error"), result.get("warning")),
            "exitCode": result.get("exitCode"),
            "timedOut": bool(result.get("timedOut")),
        }

        def mutate(data):
            container = data.setdefault("container", {})
            container["lastAction"] = as_text(action)
            container["lastActionAt"] = now_ms()
            container["lastResult"] = json.dumps(summary, ensure_ascii=False)[:500]
            if action in ("stop", "restart") and summary.get("ok"):
                container["pid"] = 0
        self.state_store.update(mutate)

    # ---- 控制面分发 ----

    def handle_api(self, method, path, query, body):
        if method == "GET":
            if path in ("/", "/health"):
                return 200, self.build_health()
            if path == "/status":
                force = first_non_blank((query.get("refresh") or [""])[0]).lower() in ("1", "true", "yes")
                return 200, self.build_status(force=force)
            if path == "/config":
                return 200, self._masked_config_payload()
            if path == "/logs":
                lines = safe_int((query.get("lines") or ["200"])[0], 200)
                text = self.logs_text(lines)
                return 200, {
                    "ok": True,
                    "text": text,
                    "lines": len(text.splitlines()) if text else 0,
                    "logFile": self.log_file,
                }
            if path == "/ping":
                return 200, {"ok": True, "pong": now_ms()}
            return 404, {"ok": False, "error": "not_found", "path": path}
        if method == "POST":
            if path == "/config":
                if not is_object(body):
                    return 400, {"ok": False, "error": "body_must_be_object"}
                if not body:
                    return 400, {"ok": False, "error": "empty_config_patch"}
                try:
                    self.save_config_patch(body)
                except ValueError as error:
                    return 400, {"ok": False, "error": as_text(error)}
                return 200, self._masked_config_payload()
            if path == "/auto":
                enabled = to_bool(body.get("enabled"), False)
                auto_status = self.set_auto_enabled(enabled)
                self.log("自动回复开关 -> %s" % ("开" if enabled else "关"))
                return 200, {"ok": True, "autoReply": auto_status}
            if path == "/whitelist":
                try:
                    return 200, self.set_whitelist(body if is_object(body) else {})
                except ValueError as error:
                    return 400, {"ok": False, "error": as_text(error)}
            if path == "/container/action":
                action = first_non_blank(body.get("action")).lower()
                if action not in ("status", "pull", "create", "setup", "start", "stop", "restart", "logs"):
                    return 400, {"ok": False, "error": "unsupported_action", "action": action}
                lines = safe_int(body.get("lines"), 200)
                outcome = self.container_action(action, lines)
                payload = {
                    "ok": bool(outcome.get("ok")),
                    "result": outcome.get("result", {}),
                    "log": as_text(outcome.get("log")),
                }
                error = first_non_blank(outcome.get("error"))
                if error:
                    payload["error"] = error
                return 200, payload
            if path == "/send":
                scene = first_non_blank(body.get("scene")).lower()
                target = first_non_blank(body.get("target"))
                text = as_text(body.get("text"))
                if scene not in ("private", "group"):
                    return 400, {"ok": False, "error": "scene 必须是 private 或 group"}
                if not target:
                    return 400, {"ok": False, "error": "target 不能为空"}
                if not text.strip():
                    return 400, {"ok": False, "error": "text 不能为空"}
                result = self.send_message_now(scene, target, text)
                self.log("手动发送 %s -> %s: ok=%s" % (scene, target, result.get("ok")))
                payload = {"ok": bool(result.get("ok")), "result": result}
                if not result.get("ok"):
                    payload["error"] = first_non_blank(result.get("error"))
                return 200, payload
            if path == "/restart-bridge":
                return 200, self.restart_bridge()
            if path == "/test-deepseek":
                return 200, self.test_deepseek(first_non_blank(body.get("text")))
            if path == "/shutdown":
                timer = threading.Timer(0.3, lambda: self.request_shutdown("http_shutdown"))
                timer.daemon = True
                timer.start()
                self.log("收到 /shutdown，准备退出")
                return 200, {"ok": True, "shuttingDown": True}
            return 404, {"ok": False, "error": "not_found", "path": path}
        return 405, {"ok": False, "error": "method_not_allowed", "method": method, "path": path}


# ---------------------------------------------------------------------------
# 控制 HTTP 服务
# ---------------------------------------------------------------------------

class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_control_handler(service):
    class ControlHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SnowLumaBridge/" + VERSION
        sys_version = ""

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            try:
                if not self._authorized():
                    self._write_json(401, {"ok": False, "error": "unauthorized",
                                           "message": "缺少或错误的 X-Auth-Token"})
                    return
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path or "/"
                if len(path) > 1:
                    path = path.rstrip("/") or "/"
                query = urllib.parse.parse_qs(parsed.query)
                body = {}
                if method == "POST":
                    try:
                        body = self._read_json_body()
                    except ValueError as error:
                        self._write_json(400, {"ok": False, "error": "invalid_json", "message": as_text(error)})
                        return
                status_code, payload = service.handle_api(method, path, query, body)
                self._write_json(status_code, payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                service.log("控制面异常 %s %s: %s: %s" % (method, self.path, error.__class__.__name__, error))
                try:
                    self._write_json(500, {"ok": False, "error": "internal_error",
                                           "message": "%s: %s" % (error.__class__.__name__, error)})
                except Exception:
                    pass

        def _authorized(self):
            token = as_text(service.control_token)
            if not token:
                return True
            provided = first_non_blank(self.headers.get("X-Auth-Token"))
            if not provided:
                parsed = urllib.parse.urlsplit(self.path)
                provided = first_non_blank((urllib.parse.parse_qs(parsed.query).get("token") or [""])[0])
            if not provided:
                authorization = first_non_blank(self.headers.get("Authorization"))
                if authorization.lower().startswith("bearer "):
                    provided = authorization[7:].strip()
            if not provided:
                return False
            try:
                return hmac.compare_digest(provided, token)
            except (TypeError, ValueError):
                return False

        def _read_json_body(self):
            length = safe_int(self.headers.get("Content-Length"), 0)
            if length <= 0:
                return {}
            if length > 4 * 1024 * 1024:
                raise ValueError("body_too_large")
            raw = self.rfile.read(length).decode("utf-8", errors="replace").strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            if not is_object(parsed):
                raise ValueError("body_must_be_object")
            return parsed

        def _write_json(self, status_code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.close_connection = True

        def log_message(self, _format, *_args):
            return

    return ControlHandler


# ---------------------------------------------------------------------------
# 进程入口
# ---------------------------------------------------------------------------

def install_process_hooks(service):
    def handle_thread_exception(args):
        try:
            service.log("线程未捕获异常 %s: %s\n%s" % (
                getattr(args.exc_type, "__name__", "Exception"),
                args.exc_value,
                "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))[:4000],
            ))
        except Exception:
            pass

    threading.excepthook = handle_thread_exception
    original_hook = sys.excepthook

    def handle_uncaught(exc_type, exc_value, exc_traceback):
        try:
            service.log("进程未捕获异常 %s: %s\n%s" % (
                getattr(exc_type, "__name__", "Exception"),
                exc_value,
                "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))[:4000],
            ))
        except Exception:
            pass
        try:
            original_hook(exc_type, exc_value, exc_traceback)
        except Exception:
            pass

    sys.excepthook = handle_uncaught


def write_crash_log(state_dir, message, traceback_text):
    try:
        ensure_dir(state_dir)
        crash_path = os.path.join(state_dir, "crash.log")
        with open(crash_path, "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n%s\n" % (iso_now(), message, traceback_text))
        chmod_quiet(crash_path, 0o600)
    except OSError:
        pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Operit SnowLuma 桥接服务：控制 HTTP + OneBot v11 WS 客户端 + DeepSeek 自动回复")
    parser.add_argument("--state-dir", default="",
                        help="插件数据目录（config.json/state.json/bridge.log 所在目录）")
    parser.add_argument("--port", type=int, default=DEFAULT_CONTROL_PORT,
                        help="控制 HTTP 端口（默认 32146，仅绑定 127.0.0.1）")
    parser.add_argument("--token", default="",
                        help="控制面鉴权 token（X-Auth-Token 校验）；同时作为 config.bridgeToken 的内存回填值")
    parser.add_argument("--log-file", default="", help="日志文件路径（默认 <state-dir>/bridge.log）")
    parser.add_argument("--udocker-binary", default="", help="udocker 可执行文件路径（默认自动探测）")
    parser.add_argument("--version", action="store_true", help="打印版本号并退出")
    args, unknown = parser.parse_known_args(argv)
    if not args.version and not first_non_blank(args.state_dir):
        parser.error("--state-dir 不能为空")
    return args, unknown


def main(argv=None):
    args, unknown = parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    service = None
    try:
        service = SnowLumaService(args)
        if unknown:
            service.log("忽略未知启动参数: %s" % " ".join(unknown))
        install_process_hooks(service)

        def _handle_signal(signum, _frame):
            service.request_shutdown("signal_%d" % signum)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        service.start()
        service.run_forever()
        return 0
    except Exception as error:
        message = "%s: %s" % (error.__class__.__name__, error)
        traceback_text = traceback.format_exc()
        try:
            if service is not None:
                service.log("服务崩溃: %s\n%s" % (message, traceback_text[:8000]))
            else:
                sys.stderr.write("snowluma_service start failed: %s\n%s\n" % (message, traceback_text))
                sys.stderr.flush()
        except Exception:
            pass
        write_crash_log(os.path.abspath(as_text(args.state_dir) or "."), message, traceback_text)
        return 1
    finally:
        if service is not None:
            try:
                service.shutdown()
            except Exception as error:
                try:
                    sys.stderr.write("shutdown error: %s\n" % error)
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())

