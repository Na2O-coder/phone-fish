# 11 · 给 DSH / 其他 Agent 的安装调试手册

> 面向：在手机上运行的 DSH 或其他有 shell/SSH/文件能力的 Agent。
> 目标：让 Agent 按步骤协助主人装 DroidVM、VM、SnowLuma、bridge，并在出问题时按决策树排查。
>
> 重要：Agent 不是“代你点完所有按钮”的黑盒。SnowLuma 的 EULA 仍要求用户自己取得软件并遵守授权；Agent 不要自动下载/再分发 SnowLuma tar，也不要把它上传到公开仓库。

## 0. Agent 的角色

Agent 可以做：

- 通过 SSH 在 VM 里执行通用 Linux 命令；
- 检查内存/磁盘/端口/服务/日志；
- 帮用户把 bridge 包放到 /opt/snowluma/bridge；
- 运行 bootstrap、systemd、healthcheck；
- 根据报错查文档、给下一步。

Agent 不要做：

- 不要替用户申请/填写 QQ 密码、扫码；
- 不要把 API key/token/QQ 号打印到聊天/日志/截图；
- 不要在没有确认时执行 rm -rf、覆盖 /opt/snowluma、格式化磁盘；
- 不要下载/分发 SnowLuma 二进制或 tar 到公开位置；
- 不要绕过 SnowLuma EULA。

## 1. 先收集这些信息（缺一不可）

Agent 先问主人，而不是猜：

| 信息 | 示例/说明 |
|---|---|
| DroidVM 是否已装、VM 是否启动 | 是/否 |
| VM 发行版/用户 | Ubuntu，用户 ubuntu 或自定义 |
| SSH 入口 | 手机 127.0.0.1:2222 → VM 22 |
| VM 内存/磁盘 | free -h / df -h |
| SnowLuma 是否已安装 | 是/否/只下载了 tar |
| WebUI/OneBot 端口 | 5099 / 3001 |
| OneBot token | 主人自己输入，不贴聊天 |
| 主人 QQ / bot QQ | 只填数字 |
| DeepSeek key | 主人自己输入，不贴聊天 |
| 是否接受小号风险 | 必须明确同意 |

## 2. 阶段 A：手机/VM 预检

在手机端 Agent 可用 SSH 时：

~~~bash
ssh -p 2222 <用户>@127.0.0.1
~~~

在 VM 内执行：

~~~bash
uname -a
free -h
df -h /
nproc
cat /etc/os-release | head
ip -4 addr show | grep inet
ss -ltnp | grep -E '2222|5099|3001|32146' || true
~~~

通过标准：

- 内核是 arm64；
- 可用内存 ≥ 1.5 GB，磁盘空余 ≥ 8 GB；
- SSH 能进；
- 5099/3001 还没被别的进程占用。

不通过就停，先解决：

- 手机杀后台 → 关电池优化/允许后台；
- 内存不足 → VM 改 3 GB，清后台；
- 磁盘不足 → 删旧 rootfs/日志/备份；
- SSH 不通 → DroidVM 端口转发/sshd。

## 3. 阶段 B：取 SnowLuma 镜像（主人操作 GitHub）

Agent 不应替主人跑公开镜像下载。让主人按 [10-SnowLuma镜像获取-DockerTarBuilder.md](10-SnowLuma镜像获取-DockerTarBuilder.md)：

1. 用自己的 GitHub fork；
2. Actions → ARM64 Pull and Save Docker Image；
3. 输入 motricseven7/snowluma:v2.1.1；
4. 下载 Artifact；
5. 传到 VM 的 /tmp。

Agent 只做校验：

~~~bash
ls -lh /tmp/*snowluma*.tar.gz
gzip -t /tmp/*snowluma*.tar.gz
~~~

不要把它上传/提交/发群。

## 4. 阶段 C：VM 初始化（Agent 可执行）

~~~bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv sqlite3 curl unzip ca-certificates openssh-server skopeo umoci
sudo systemctl enable --now ssh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo timedatectl set-timezone Asia/Shanghai
sudo mkdir -p /opt/snowluma/bridge /opt/snowluma/data /opt/snowluma/config /opt/snowluma/local-share
sudo chown -R "$USER:$USER" /opt/snowluma
~~~

如果 VM 用户名不是 ubuntu，后面 systemd unit 的 User= 要改成实际用户。

## 5. 阶段 D：SnowLuma 本地安装

Agent 只做辅助，不提供/不下载 SnowLuma 本体。

按主人下载的官方 tar：

~~~bash
cd /tmp
gzip -dc snowluma*.tar.gz > snowluma-image.tar
skopeo inspect docker-archive:/tmp/snowluma-image.tar | head
skopeo copy docker-archive:/tmp/snowluma-image.tar oci:/tmp/snowluma-oci:v2.1.1
umoci unpack --image /tmp/snowluma-oci:v2.1.1 /tmp/snowluma-unpack
ls -ld /tmp/snowluma-unpack/rootfs
~~~

确认是全新安装后（**先问主人**）：

~~~bash
sudo mv /opt/snowluma-root /opt/snowluma-root.bak.$(date +%s) 2>/dev/null || true
sudo cp -a /tmp/snowluma-unpack/rootfs /opt/snowluma-root
~~~

启动/配置 SnowLuma 的方式按官方文档和主人已有环境；Agent 不要凭空写一份“官方安装脚本”。

通过标准：

- WebUI 5099 有页面；
- 主人用 QQ 小号扫码成功；
- OneBot WS 3001 监听；
- bridge 能连 3001。

## 6. 阶段 E：安装 bridge（Agent 可执行）

把本包放到 VM，例如 /opt/snowluma/phone-fish。

~~~bash
cd /opt/snowluma/phone-fish
python3 tools/bootstrap.py
~~~

Agent 引导主人在终端里**自己输入**：

- owner QQ；
- bot QQ（可空）；
- DeepSeek key；
- OneBot token。

不要让主人把 key 贴到聊天里。bootstrap 会生成 config.json / bridge.env（600）。

~~~bash
sudo mkdir -p /opt/snowluma/bridge
sudo cp -a bridge/. /opt/snowluma/bridge/
sudo chown -R "$USER:$USER" /opt/snowluma/bridge
sudo cp /opt/snowluma/phone-fish/vm/systemd/snowluma-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now snowluma-bridge
sudo systemctl status snowluma-bridge --no-pager
~~~

验证：

~~~bash
curl -s -H "X-Auth-Token: 主人输入的BRIDGE_CONTROL_TOKEN" http://127.0.0.1:32146/health
journalctl -u snowluma-bridge -n 80 --no-pager
~~~

通过标准：

- /health 返回 ok；
- 日志出现 OneBot WebSocket 已连接；
- 私聊 bot 能回一句。

## 7. 阶段 F：调试决策树

按顺序排查，不要一次性改很多地方。

### 7.1 VM / SSH

- 手机连不上 2222：DroidVM 端口转发、VM 启动、sshd；
- VM 很卡：内存 3 GB、清后台、关不必要的服务；
- 重启后 VM 没起来：DroidVM 是否开机启动、电池优化。

### 7.2 WebUI / QQ

- 5099 打不开：服务没起/端口转发；
- 扫码后掉线：不要官方 QQ 同号登录；网络稳定；少重建；
- WebUI 能开但 OneBot 没事件：OneBot 配置/端口/白名单。

### 7.3 OneBot / bridge

~~~bash
ss -ltnp | grep 3001
curl -s http://127.0.0.1:3000/get_login_info?access_token=<token>
journalctl -u snowluma-bridge -n 100 --no-pager
~~~

- 401：token 不一致；
- WS 连不上：host/port/token；
- 控制面 401：X-Auth-Token 与 bridge.env 不一致；
- 端口占用：停重复进程。

### 7.4 不回复

- autoReply 开没开；
- 白名单/冷却/睡眠；
- 是否 @ 到/引用到；
- 群聊 groupReply 配置；
- 日志里有没有“自动回复成功”。

### 7.5 成本/缓存

- 输出高：thinking 模式；low 任务应关闭；
- 缓存 miss：切换 thinking/重启后的首次冷启动，不是坏了；
- 不要用关键词黑名单省 token。

## 8. 阶段 G：交付与安全

Agent 结束时输出：

- 已完成的阶段；
- 当前服务状态/端口；
- 未完成项；
- 风险（小号、SnowLuma EULA、密钥轮换）；
- 回滚方式（停 bridge、恢复备份）。

不要输出：

- key/token/密码/QQ 号；
- 完整配置；
- 私人聊天/截图；
- 公开 tar/rootfs。

## 9. 给 Agent 的提示词模板

~~~text
你是在安卓手机上协助主人部署 QQ AI 小号的工程 Agent。
请严格按仓库 docs/11 的步骤执行：
1. 先收集信息，不要猜密码/token/QQ；
2. 先做只读预检（uname/free/df/ss），通过后再改；
3. SnowLuma 本体由主人自己从官方渠道取得，你不要下载/分发；
4. 任何 rm -rf、覆盖 /opt、systemctl 重启前先向主人确认；
5. 不要把 key/token/QQ/密码写进日志或聊天；
6. 每阶段给通过标准和失败时的下一步；
7. 遇到不明确的地方停下来问，不要编造命令。
~~~

## 10. 最小验收

- [ ] SSH 可进
- [ ] WebUI 5099 可访问
- [ ] QQ 小号在线
- [ ] OneBot 3001 监听
- [ ] /health ok
- [ ] 私聊能回
- [ ] 群里 @ 能回
- [ ] 重启后自动恢复
- [ ] 备份/恢复演练
