#!/usr/bin/env python3
"""从 staging 生成公开源码包、梗库资产和 SHA256SUMS。"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.3"
INCLUDE_DIRS = ["docs", "bridge", "vm", "tools", "memewiki", "tests"]
INCLUDE_FILES = ["README.md", "AGENTS.md", "LICENSE", "NOTICE.md", "SECURITY.md", "PRIVACY.md", "CHANGELOG.md", "ACKNOWLEDGEMENTS.md", ".gitignore"]
SKIP_NAMES = {"config.json", "bridge.env", "state.json", "memory.db", "conversation.db", "bridge.log"}
SKIP_DIRS = {"__pycache__", "dist", ".git"}


def collect():
    files = []
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
    for name in INCLUDE_DIRS:
        directory = ROOT / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name in SKIP_NAMES or path.suffix in {".pyc", ".log"}:
                continue
            files.append(path)
    return sorted(set(files))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default=str(ROOT.parent / "dist"))
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    scan = subprocess.run([sys.executable, str(ROOT / "tools/redact_check.py"), str(ROOT)], text=True)
    if scan.returncode != 0:
        print(scan.stdout)
        print(scan.stderr)
        raise SystemExit("redact_check failed")

    files = collect()
    manifest = {
        "name": "phone-fish",
        "version": VERSION,
        "files": [],
        "note": "SnowLuma/DroidVM/QQ 账号数据不包含在此包中。",
    }
    archive_path = out_dir / ("phone-fish-v%s.zip" % VERSION)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            archive.write(path, rel)
            manifest["files"].append({"path": rel, "bytes": path.stat().st_size, "sha256": sha256(path)})

    manifest_path = out_dir / ("phone-fish-v%s.manifest.json" % VERSION)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assets = []
    memes_src = ROOT / "assets" / "memes.db"
    if memes_src.is_file():
        memes_dst = out_dir / "memes.db"
        shutil.copy2(memes_src, memes_dst)
        assets.append(memes_dst)
        notice = out_dir / "memewiki-LICENSE.txt"
        notice.write_text(
            "memes.db 数据来源：in1t1a1/astrbot_plugin_memewiki，经 thawline/dsh-memewiki 整理。\n"
            "MIT License；详见仓库 memewiki/LICENSE。\n", encoding="utf-8")
        assets.append(notice)

    sums = []
    for path in [archive_path, manifest_path] + assets:
        sums.append("%s  %s" % (sha256(path), path.name))
    sums_path = out_dir / ("SHA256SUMS-v%s.txt" % VERSION)
    sums_path.write_text("\n".join(sums) + "\n", encoding="utf-8")

    print("[build] archive:", archive_path, archive_path.stat().st_size, "bytes")
    print("[build] files:", len(files))
    print("[build] manifest:", manifest_path)
    for line in sums:
        print("[build]", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
