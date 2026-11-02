# AGENTS.md

如果你是在手机上协助主人部署本项目的 Agent，先读：

- docs/11-给DSH或Agent的安装调试手册.md
- docs/10-SnowLuma镜像获取-DockerTarBuilder.md
- docs/01 ~ docs/08

## 硬性规则

1. 先只读预检，再改系统；任何 rm -rf、覆盖 /opt、格式化、systemctl 重启前先让主人确认。
2. 不猜密码、token、QQ、API key；让主人自己在终端输入，不要贴到聊天。
3. 不把密钥/token/QQ/私人聊天写进日志、截图、commit。
4. SnowLuma 本体由主人自己从官方渠道取得；你不下载/再分发 tar/rootfs，不上传公开 Release。
5. 遵循 SnowLuma EULA 和 QQ 平台条款；只用小号，非商业。
6. 不做“万能一键脚本”；按 docs/11 阶段给通过标准和失败下一步。
7. 先安全、后功能；不确定就停下来问。

## 快速路径

~~~text
预检 SSH/内存/磁盘
→ 主人用 DockerTarBuilder 取官方镜像
→ VM 通用初始化
→ 主人按官方文档安装/登录 SnowLuma
→ bridge bootstrap + systemd
→ /health + OneBot WS + 私聊测试
→ 备份/隐私检查
~~~

## 交付格式

结束时报告：完成阶段、服务状态、未完成项、风险、回滚方式；不要输出任何 secret。
