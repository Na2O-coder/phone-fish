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

## Architecture Constraints

The project uses the following layers:

~~~text
Android Host
└── DroidVM (VM manager)
    ├── hardware virtualization backend: Gunyah / GenieZone / KVM
    └── Ubuntu VM
        ├── SnowLuma (official rootfs via chroot)
        │   └── OneBot HTTP :3000 / WS :3001 / WebUI :5099
        └── snowluma-bridge
            └── DeepSeek API
~~~

Important:

1. DroidVM is the VM manager/runtime, not the Hypervisor itself.
2. Gunyah, GenieZone and KVM are hardware virtualization backends; KVM is not a separate VM application.
3. SnowLuma runs its official rootfs through chroot inside Ubuntu.
4. snowluma-bridge is a separate project component running in Ubuntu.
5. The bridge connects to SnowLuma through the OneBot WebSocket interface.
6. Do not assume Docker is part of the current runtime. OCI/Docker image tools may be used only to unpack the official rootfs; the runtime is chroot.
7. Do not add Docker Engine commands to deployment instructions unless a specific documented component actually requires Docker.
8. Follow SnowLuma's current EULA and official installation instructions.

## 虚拟化路线选择

部署前必须先判断设备适合哪条路线，不要默认 DroidVM/硬件虚拟化一定可用。

先在手机宿主机做只读检查（需要 Root shell）：

~~~bash
su
ls -l /dev/gunyah /dev/gzvm /dev/kvm 2>/dev/null
uname -a
getprop ro.product.model
getprop ro.board.platform
getprop ro.build.version.release
~~~

按设备节点初判后端：

- `/dev/gunyah` 存在：Qualcomm Gunyah；
- `/dev/gzvm` 存在：MediaTek GenieZone；
- `/dev/kvm` 存在：Linux KVM；
- 都不存在：软件模拟路线（Experimental）。

注意：节点存在只说明接口可用，不代表 DroidVM 一定能启动 VM；必须实际启动一个最小 VM 验证。不要只根据 SoC 型号判断。

规则：

- 有 Root + 可用的硬件虚拟化后端：优先硬件虚拟化。
- 没有 Root / 没有可用后端：说明软件模拟是 Experimental，性能和续航可能明显变差；不要描述成“和硬件虚拟化一样”。
- 不要因为处理器是骁龙 8 Gen 3 / 8 Elite 就直接认定支持某种后端。
- VM 启动失败时，不要反复重跑同一套命令；记录错误，问主人是否尝试软件模拟路线。
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
硬件虚拟化后端：
设备节点/权限：
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
先读 docs/00 + docs/12，判断硬件虚拟化后端还是软件模拟
→ 只读预检 SSH/内存/磁盘/虚拟化
→ 硬件虚拟化：DroidVM + Ubuntu VM
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
