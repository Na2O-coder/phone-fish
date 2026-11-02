#!/bin/sh
# Ubuntu VM 通用初始化：不安装、不分发 SnowLuma。
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo sh vm/setup-vm.sh 运行" >&2
  exit 1
fi
apt-get update
apt-get install -y python3 python3-venv sqlite3 curl unzip ca-certificates openssh-server
systemctl enable --now ssh
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
mkdir -p /opt/snowluma/bridge /opt/snowluma/data /opt/snowluma/config /opt/snowluma/local-share
chown -R ubuntu:ubuntu /opt/snowluma
printf '%s\n' "Asia/Shanghai" > /etc/timezone
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
echo "[setup] VM 基础依赖完成；SnowLuma 请按官方文档安装"
echo "[setup] 下一步：把 release 里的 bridge/ 放到 /opt/snowluma/bridge，然后运行 sudo python3 tools/bootstrap.py"
