# 安全说明

## 不要提交的东西

- .env、bridge.env、config.json、state.json；
- memory.db、conversation.db、bridge.log、备份；
- 任何 API key、OneBot token、bridge token、QQ 号、群号、IP、密码、cookie；
- 真实聊天截图/头像/语音/视频。

## 密钥管理

- 使用 tools/bootstrap.py 生成配置，文件权限 600；
- 控制 API 只监听 127.0.0.1，并设置随机 BRIDGE_CONTROL_TOKEN；
- 不要把 token 写进 systemd unit 或 Git；
- 怀疑泄露时立即轮换 DeepSeek/ASR/OneBot/bridge token 和 SSH/VNC 密码；
- 发布视频前检查终端窗口、二维码、通知栏、浏览器标签和 API 控制台。

## 报告问题

如果你在本仓库发现隐私/密钥泄露，请开 issue 或联系维护者，不要直接贴出密钥。发布前请运行：

~~~bash
python3 tools/redact_check.py .
~~~
