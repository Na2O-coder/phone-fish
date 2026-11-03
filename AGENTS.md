# AGENTS.md

如果你是在手机上协助主人部署本项目的 Agent，先读：

- docs/14-给DSH或Agent的安装调试手册.md
- docs/11-SnowLuma镜像获取-DockerTarBuilder.md
- docs/00-选择虚拟机运行路线.md
- docs/02 ~ docs/09

## 硬性规则

1. 先只读预检，再改系统；任何 rm -rf、覆盖 /opt、格式化、systemctl 重启前先让主人确认。
2. 不猜密码、token、QQ、API key；让主人自己在终端输入，不要贴到聊天。
3. 不把密钥/token/QQ/私人聊天写进日志、截图、commit。
4. SnowLuma 本体由主人自己从官方渠道取得；你不下载/再分发 tar/rootfs，不上传公开 Release。
5. 遵循 SnowLuma EULA 和 QQ 平台条款；只用小号，非商业。
6. 不做“万能一键脚本”；按 docs/14 阶段给通过标准和失败下一步。
7. 先安全、后功能；不确定就停下来问。

## 虚拟化路线选择

部署前必须先判断设备适合哪条路线，不要默认 DroidVM/KVM 一定可用。

先检查：

- Android 设备型号、CPU 架构、Android 版本；
- Root 状态；
- 内核版本；
- /dev/kvm 是否存在、当前用户是否有权限；
- DroidVM 能否实际启动一个最小 VM。

规则：

- 有 Root + 可用 KVM：优先 KVM。
- 没有 Root 或没有 KVM：说明软件模拟是 Experimental，性能和续航可能明显变差；不要描述成“和 KVM 一样”。
- 不要因为处理器是骁龙 8 Gen 3 / 8 Elite 就直接认定支持 KVM，必须以实际检查结果为准。
- KVM 启动失败时，不要反复重跑同一套 KVM 命令；记录错误，问主人是否尝试软件模拟路线。
- 两条路线不要混用初始化命令。

## SnowLuma 授权边界

SnowLuma 是独立第三方软件，不属于 phone-fish。

Agent 必须：

1. 提醒主人阅读 SnowLuma 当前 EULA。
2. 不擅自下载、分发或上传 SnowLuma 本体。
3. 不绕过官方授权要求。
4. 不把 SnowLuma 本体打包进 phone-fish Release。
5. 不擅自执行可能违反 EULA 的自动化部署操作。
6. 涉及安装、镜像获取、运行方式时，遵循当前官方授权允许的范围。

Agent 可以：

- 检查主人已经取得的文件；
- 检查 Linux 环境、目录、端口和依赖；
- 协助部署 phone-fish 自身允许自动化的部分；
- 检查 OneBot 和 bridge；
- 排查服务运行问题。

如果某项操作是否符合 EULA 不明确：停止操作并向主人说明。

## 操作分级

| 等级 | 操作 | 是否确认 |
|---|---|---|
| Level 1 | 检查系统、看日志、查端口、只读采样 | 通常不需要 |
| Level 2 | 安装依赖、配置 bridge、创建 systemd 服务 | 按影响范围确认 |
| Level 3 | 格式化/覆盖、删除数据、QQ 登录、密钥输入、第三方软件授权 | 必须确认或由主人亲自操作 |

## 环境检测报告

正式部署前，先输出一份可复制的报告：

~~~text
PHONE-FISH 环境检测
设备：
CPU：
Root：
KVM：
KVM 权限：
内存：
可用存储：
SSH：
虚拟机运行方式：
推荐路线：
警告：
是否继续：
~~~

如果推荐软件模拟，必须写明：性能损耗、发热、耗电可能增加，且当前为实验性。

## 快速路径

~~~text
先读 docs/00 + docs/12，判断 KVM 还是软件模拟
→ 只读预检 SSH/内存/磁盘/虚拟化
→ KVM：DroidVM + Ubuntu VM
→ 软件模拟：Experimental，先说明限制
→ 主人用 DockerTarBuilder 取官方镜像
→ VM 通用初始化
→ 主人按官方文档安装/登录 SnowLuma
→ bridge bootstrap + systemd
→ /health + OneBot WS + 私聊测试
→ 备份/隐私检查
~~~

## 交付格式

结束时报告：完成阶段、服务状态、未完成项、风险、回滚方式；不要输出任何 secret。
