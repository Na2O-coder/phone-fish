#!/bin/sh
# 在临时目录用备用端口启动 bridge，检查控制面 /health 后退出；不碰线上状态。
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
PORT="32147"
cleanup() {
  if [ -n "${PID:-}" ]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM
mkdir -p "$TMP"
cp -a "$ROOT/bridge" "$TMP/bridge"
cp -a "$ROOT/tools" "$TMP/tools"
python3 "$TMP/tools/bootstrap.py" --owner 10000002 --bot-qq 10000001 --key sk-test-not-real --onebot-token test-onebot --bridge-token test-control --yes >/dev/null
cd "$TMP/bridge"
python3 snowluma_service.py --state-dir "$TMP/bridge" --port "$PORT" --token test-control >"$TMP/service.log" 2>&1 &
PID=$!
python3 - "$PORT" <<'PY'
import sys, time, urllib.request
port = int(sys.argv[1])
last = None
for _ in range(40):
    try:
        request = urllib.request.Request("http://127.0.0.1:%d/health" % port, headers={"X-Auth-Token": "test-control"})
        with urllib.request.urlopen(request, timeout=1) as response:
            body = response.read().decode("utf-8", "replace")
        print("[smoke] /health ->", body[:160])
        sys.exit(0)
    except Exception as error:
        last = error
        time.sleep(0.25)
print("[smoke] failed:", last)
sys.exit(1)
PY
echo "[smoke] OK"
