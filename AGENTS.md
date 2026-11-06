# AGENTS.md

如果你是在手机上协助主人部署本项目的 Agent，先读：

- docs/14-给DSH或Agent的安装调试手册.md
- docs/11-SnowLuma镜像获取-DockerTarBuilder.md
- docs/00-选择虚拟机运行路线.md
- docs/02 ~ docs/09

> **phone-fish 把 Agent 辅助部署/调试作为一等能力。** 主要面向手机上的 dshgo / DSH：用户不必先学会整套 Linux，Agent 负责只读预检、分阶段部署、验收和排错；用户负责必须由本人完成的授权、账号登录、人设和私密配置。Agent 仍然不是“代点所有按钮”的黑盒，不能跳过 SnowLuma EULA 和确认步骤。

## Agent 辅助部署是核心路径

推荐给用户的默认路径：

1. 用户在手机上打开 dshgo / DSH，把本文件（AGENTS.md）和 docs/14 交给 Agent；
2. Agent 先做只读环境预检，输出环境报告，不直接改系统；
3. Agent 按阶段部署：虚拟化路线 → VM → SnowLuma → OneBot → bridge → 验收；
4. 每一阶段给出通过标准、失败下一步和回滚方式；
5. 部署完成后，Agent 把需要用户确认/填写的配置列成清单，不把密钥写进公开文件；
6. 用户只用小号、自己完成 SnowLuma 授权和 QQ 登录，Agent 不代登录、不猜密码。

Agent 的交付目标是：主人能在不完全理解 Linux 的情况下，知道“现在到哪一步、下一步填什么、怎么验证、怎么恢复”。

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

## 用户部署前要准备、部署后要交给 Agent 的信息

Agent 应主动列清单，但不要要求用户把 secret 贴到群聊、公开 issue 或截图里。优先让用户在本机配置文件/私密输入中填写，Agent 只读取、脱敏日志、写盘后 chmod 600。

| 信息 | 用途 | 建议交接方式 |
|---|---|---|
| SnowLuma 官方 rootfs / 镜像来源 | 运行 QQ | 用户自己取得并确认 EULA；Agent 只检查路径和完整性 |
| QQ 小号、主人 QQ、群号 | 登录、权限、白名单 | 账号密码由用户自己登录；QQ 号/群号可私密给 Agent |
| 人设提示词 | 决定说话风格、称呼、边界 | 用户提供人设文本或偏好；Agent 写入 prompts/persona.md 或 config，不提交 |
| DeepSeek API key / baseUrl / model | LLM | 本机私密文件或一次性输入；不写 README/commit/日志 |
| TTS / STT 配置 | 语音；可选 | Edge TTS 通常无需 key；阿里云 ASR 等 key 由用户私密提供；Agent 只做连通性测试 |
| OneBot / bridge token | 鉴权 | 优先由 Agent 随机生成并写入 600 权限文件；用户在 SnowLuma UI 里填写，不贴群 |
| 群行为偏好 | 回不回、@、插话、睡眠、早安、主动 | 用户用自然语言给 Agent；Agent 写成 config，不写死人格 |
| 手机/VM 访问方式 | SSH、VM 控制、备份 | 用户已配置的本地凭据；Agent 不猜密码 |
| 备份位置与频率 | 恢复 | Agent 给出方案，用户确认可写路径 |

### 部署完成后必须让用户确认或填写的项

1. 主人 QQ（permissions.ownerUsers）和机器人小号；
2. 允许的群、群管理员、白名单/黑名单；
3. DeepSeek key、baseUrl、model、思考模式和预算；
4. 人设：名字、自称、对主人的称呼、语气、边界、禁用话题/行为；
5. TTS/STT：是否启用、音色、阿里云 key/endpoint、测试语音；
6. OneBot token、bridge token、控制面 token；
7. 睡眠时段、是否早晨主动消息、哪些群用 /早安 on 开启；
8. 是否启用主动找话题、日报、复盘、长期记忆、表情库；
9. 备份路径、备份频率、恢复演练；
10. 验收：/health、OneBot WS、私聊、群 @、语音、早晨开关、磁盘只读告警。

### 给 dshgo / DSH 的启动提示词

可以直接把下面这段交给 Agent：

~~~text
请以 dshgo / DSH 的身份，按 phone-fish 的 AGENTS.md 和 docs/14 帮我部署、调试。
先只读预检并输出环境报告，不要直接改系统；每个阶段先说明目的、通过标准、失败下一步。
不要下载、分发或上传 SnowLuma 本体；需要我本人登录/授权/输入密钥时停下来让我操作。
部署完成后，请列出我需要确认或填写的配置清单，并说明哪些信息属于 secret、不能提交到 GitHub。
~~~

## 交付格式

结束时报告：完成阶段、服务状态、未完成项、风险、回滚方式；不要输出任何 secret。
