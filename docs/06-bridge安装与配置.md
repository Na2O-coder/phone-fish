# 06 · bridge 安装与配置

## 1. 解压

把 release 包解压到 VM，例如：

~~~bash
cd /opt/snowluma
unzip /tmp/phone-fish-v0.1.0.zip -d phone-fish
~~~

目录里应该有：

~~~text
phone-fish/
├── bridge/
├── docs/
├── tools/
├── vm/
└── tests/
~~~

## 2. 生成配置

在解压后的包根目录运行（tools 在包根目录，不是 bridge/ 下）：

~~~bash
sudo chown -R "$USER:$USER" /opt/snowluma/phone-fish
cd /opt/snowluma/phone-fish
python3 tools/bootstrap.py
~~~

如果解压时用了 sudo，先把包目录 chown 给你自己；否则生成的 config.json 归 root，systemd 的 User=ubuntu 会读不到。

会询问：

- 主人 QQ（只填数字）；
- 机器人 QQ（可留空，登录后由 OneBot 自动识别）；
- DeepSeek API key；
- OneBot WS token；
- 自动生成 bridge control token。

生成包内 bridge/config.json 和 bridge/bridge.env（权限 600）。

## 3. 放到运行目录

~~~bash
sudo mkdir -p /opt/snowluma/bridge
sudo cp -a phone-fish/bridge/. /opt/snowluma/bridge/
sudo chown -R "$USER:$USER" /opt/snowluma/bridge
~~~

这样 config.json/bridge.env 会一起复制过去。不要覆盖自己的 state.json/memory.db；全新机器就是空的。

## 4. 安装 systemd

~~~bash
sudo cp /opt/snowluma/phone-fish/vm/systemd/snowluma-bridge.service /etc/systemd/system/
# 如果 VM 用户名不是 ubuntu，先编辑 unit 里的 User=ubuntu
sudo systemctl daemon-reload
sudo systemctl enable --now snowluma-bridge
sudo systemctl status snowluma-bridge --no-pager
~~~

查看日志：

~~~bash
journalctl -u snowluma-bridge -n 100 --no-pager
tail -f /opt/snowluma/bridge/bridge.log
~~~

## 5. 验证

~~~bash
curl -H "X-Auth-Token: 你的BRIDGE_CONTROL_TOKEN" http://127.0.0.1:32146/health
~~~

返回 ok=true 即控制面正常。

## 6. 配置项说明

| 键 | 作用 |
|---|---|
| permissions.ownerUsers | 主人 QQ；只有这些号能用敏感命令 |
| deepseek.apiKey | DeepSeek key；不要提交 |
| deepseek.model | 默认 deepseek-flash |
| deepseek.thinkingMode | auto：低强度任务关闭思考，高强度保留 |
| onebotToken | 与 SnowLuma 一致 |
| bridgeToken | 控制面 token；bootstrap 随机生成 |
| autoReply.enabled | 配置完成后才开 |
| sleep | 23:00–07:00 睡眠 |
| behavior.context | 持久上下文/压缩阈值 |

## 7. 不要做

- 不要把 config.json/bridge.env 提交到 Git；
- 不要把 32146 暴露到公网；
- 不要用主号；
- 不要直接复制别人的 state.json/memory.db；
- 不要跳过 SnowLuma 的授权说明。
