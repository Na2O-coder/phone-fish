#!/usr/bin/env python3
"""交互式生成 bridge 配置，不联网、不写入仓库。

用法：
    python3 tools/bootstrap.py
    python3 tools/bootstrap.py --dry-run
    python3 tools/bootstrap.py --owner 10000002 --key sk-test --onebot-token xxx --bot-qq 10000001 --yes
"""
import argparse
import getpass
import json
import os
import secrets
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "bridge"
EXAMPLE = BRIDGE / "config.example.json"


def mask(value):
    text = str(value or "")
    if len(text) <= 6:
        return "***" if text else ""
    return text[:3] + "***" + text[-2:]


def load_example():
    if not EXAMPLE.is_file():
        raise SystemExit("缺少 bridge/config.example.json")
    with EXAMPLE.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ask(prompt, default=""):
    value = input(prompt).strip()
    return value or default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--owner", default="")
    parser.add_argument("--bot-qq", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--onebot-token", default="")
    parser.add_argument("--bridge-token", default="")
    parser.add_argument("--time-zone", default="Asia/Shanghai")
    parser.add_argument("--yes", action="store_true", help="按参数生成，跳过多余交互")
    args = parser.parse_args()

    config = load_example()
    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "dryRun": True,
            "ownerUsers": [],
            "botQQ": "",
            "deepseekKey": "",
            "onebotToken": "",
            "bridgeToken": "",
            "note": "尚未写入 config.json/bridge.env",
        }, ensure_ascii=False, indent=2))
        return 0

    owner = args.owner or ("" if args.yes else ask("主人 QQ（只填数字）："))
    if not owner.isdigit() or len(owner) < 5:
        raise SystemExit("主人 QQ 必须是 5 位以上数字")

    bot_qq = args.bot_qq or ("" if args.yes else ask("机器人 QQ（可留空，登录后由 OneBot 自动填）：", ""))
    if bot_qq and not bot_qq.isdigit():
        raise SystemExit("机器人 QQ 必须是数字或留空")

    key = args.key or ("" if args.yes else getpass.getpass("DeepSeek API key（输入不回显）："))
    if not key.startswith("sk-"):
        raise SystemExit("DeepSeek API key 应以 sk- 开头")

    onebot = args.onebot_token or ("" if args.yes else getpass.getpass("OneBot WS token（输入不回显，可留空）："))
    bridge = args.bridge_token or secrets.token_urlsafe(24)

    config["bridgeToken"] = bridge
    config["onebotToken"] = onebot
    config["botQQ"] = bot_qq
    config.setdefault("permissions", {})["ownerUsers"] = [owner]
    config.setdefault("skills", {}).setdefault("groupAdmin", {})["adminUsers"] = [owner]
    config.setdefault("deepseek", {})["apiKey"] = key
    config["deepseek"]["thinkingMode"] = config["deepseek"].get("thinkingMode") or "auto"
    config["autoReply"]["enabled"] = True
    config["voice"]["enabled"] = False
    config["asr"]["enabled"] = False

    bridge_dir = BRIDGE
    bridge_dir.mkdir(parents=True, exist_ok=True)
    config_path = bridge_dir / "config.json"
    env_path = bridge_dir / "bridge.env"
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    env_lines = [
        "DEEPSEEK_API_KEY=" + key,
        "DEEPSEEK_BASE_URL=" + str(config["deepseek"].get("baseUrl") or "https://api.deepseek.com/v1"),
        "DEEPSEEK_MODEL=" + str(config["deepseek"].get("model") or "deepseek-flash"),
        "ONEBOT_WS_URL=ws://127.0.0.1:" + str(config.get("onebotPort") or 3001),
        "ONEBOT_TOKEN=" + onebot,
        "BRIDGE_CONTROL_TOKEN=" + bridge,
        "OWNER_QQ=" + owner,
        "BOT_QQ=" + bot_qq,
        "TIME_ZONE=" + args.time_zone,
    ]
    with env_path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(env_lines) + "\n")
    for path in (config_path, env_path):
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    print(json.dumps({
        "ok": True,
        "config": str(config_path),
        "env": str(env_path),
        "ownerUsers": [owner],
        "botQQ": bot_qq,
        "deepseekKey": mask(key),
        "onebotToken": mask(onebot),
        "bridgeToken": mask(bridge),
        "autoReply": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
