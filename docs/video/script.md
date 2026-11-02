# B 站视频脚本：纯手机养一只 QQ AI 小鲸鱼

- 时长：35–45 分钟；可剪 25 分钟精简版
- 画幅：16:9，1080p；手机录屏 + VM 录屏 + 架构图
- 风格：实录、少剪辑感、不念说明书；重点讲“为什么”和“我踩过什么坑”
- 演示账号：独立小号/演示群，全程打码
- 核心原则：不展示真实 QQ、群、API key、IP、token、私人聊天、个人人设

## 00:00–01:30 冷开场

画面：
- 手机放在桌上，屏幕里是 QQ 聊天（头像/昵称打码）。
- 对方发一句，bot 两秒后回一句短话；再发一张表情，bot 接梗。
- 切到手机桌面/VM 运行状态，证明不是电脑/云服务器。

旁白：
“你看到的这条回复，不是我在电脑上发的，也不是云服务器。就是这台安卓手机，自己跑着一个 QQ 小号，和一只 AI 小鲸鱼。”
“这期视频，我把我走了很久的来时路，和现在能复现的‘手机养鱼包’，一次性讲清楚。”
“先说明：这是小号、非官方协议工具，有账号风险；不要拿主号试。”

字幕：纯手机 / 0 电脑 / 全流程 / 非商业 / 小号风险自负

## 01:30–05:00 来时路：为什么不是 PRoot / udocker

画面：
- Operit/PRoot 终端录屏（旧的失败日志，路径/账号打码）。
- udocker 报错、Docker 起不来、卡在 entrypoint 的截图。
- DroidVM 界面。

旁白：
“我一开始也想省事：PRoot 里套 udocker，直接在手机上跑 SnowLuma。结果很快撞墙。”
“PRoot 没有真正的 namespaces、cgroups、overlayfs；Docker 守护进程根本起不来。udocker 的 proot 再套 proot，execve 不稳定，SnowLuma 的启动脚本要跑 chown、supervisord、Xvfb、QQ，一堆外部进程，很快就失败。”
“所以我换了一条更重、但更稳的路：DroidVM，真内核虚拟机。”
“这不是最省资源的方案，但对‘长期在线、要跑 QQ 客户端’这件事，真内核少了很多玄学。”

画面字：PRoot/udocker = 实验路线；真 VM = 生产路线

## 05:00–06:30 架构

画面：动画/静态架构图，从手机到 VM 到 SnowLuma 到 bridge 到 DeepSeek。

旁白：
“最终架构是四层：安卓手机、DroidVM 里的 Ubuntu、Ubuntu 里的 SnowLuma chroot，以及我写的 bridge。”
“SnowLuma 负责 QQ 协议、扫码登录、WebUI 和 OneBot；bridge 负责判断该不该回、怎么回、记忆、关系、日报，以及调用 DeepSeek。”
“为什么用 chroot 不用 Docker？因为 DroidVM 当前内核缺 overlay/iptables/fuse，Docker 起不来；把官方 rootfs 取出来，用 chroot 跑，效果接近容器，还省了 vfs 的十几 GB。”

## 06:30–08:30 前置条件与劝退

画面：清单表格逐条出现。

旁白：
“先劝退：不是所有手机都能玩。”
“Android 13 以上、已 root、SoC/内核支持 Gunyah/GenieZone/KVM；至少 8 GB 内存、20 GB 空间；一台全新 QQ 小号；DeepSeek key 和余额。”
“还需要你能接受：非官方 QQ 协议工具可能违反平台条款，可能封号。只用小号，别用主号。”
“SnowLuma 还有自己的 EULA：非商业、源码可见，不能随便打包/自动部署/公开发布衍生版。这个包不包含它的二进制/rootfs，视频里也不提供下载。”

## 08:30–14:00 DroidVM 建 VM

画面：
- DroidVM 官方界面（不展示安装包来源以外的隐私）。
- 新建 Ubuntu VM：CPU 2–4、内存 3–4 GB、磁盘 25 GB。
- NIC 端口转发：2222/5099/3000/3001/6081。
- 共享目录（可选）。
- 启动 VM，VNC/终端。

旁白：
“VM 参数不用追求大：3–4 GB 内存、2–4 核、25 GB 盘。手机内存小就用 3 GB，别一边跑 VM 一边开一堆后台。”
“端口转发是给手机访问 VM 用的：2222 SSH、5099 WebUI、6081 noVNC；3001 是 OneBot，通常只给 VM 内部 bridge 用，不要暴露公网。”
“建好之后先在 VM 里装 SSH、开时区、禁休眠。手机端用 SSH 连 127.0.0.1:2222，就可以全程在手机上操作。”

画面字：DroidVM 官方渠道安装；不要用来源不明的 APK

## 14:00–20:00 SnowLuma 安装与 QQ 登录

画面：
- VM 终端（命令打码/占位）。
- SnowLuma WebUI 登录页；二维码用大块马赛克。
- 登录后 WebUI 状态。
- OneBot 设置：3001、token（打码）。

旁白：
“SnowLuma 这一步请按官方文档来，不同版本/内核的步骤可能不一样。这个包不代分发，也不提供一键脚本。”
“装好以后打开 WebUI，用 QQ 小号扫码。第一次登录可能要验证码，尽量在常用网络下完成，登录成功后别频繁重建环境。”
“然后在 OneBot 里开 WebSocket 3001，设一个随机 token。bridge 会连 127.0.0.1:3001。”
“注意：二维码、账号、token 全部打码；不要给观众看到你的号。”

## 20:00–26:00 装 bridge

画面：
- 解压 release 包。
- 目录结构。
- 运行 bootstrap.py，输入 owner QQ/API key/token（全部打码）。
- systemctl 启动；日志出现 OneBot WS 已连接。
- curl /health。

旁白：
“bridge 是这个包里我自己的部分。解压后把 bridge/ 放到 /opt/snowluma/bridge。”
“运行 bootstrap.py，它会问你主人 QQ、机器人 QQ、DeepSeek key、OneBot token；自动生成 config.json 和 bridge.env，权限 600。”
“然后装 systemd，启动，看日志。控制面 /health 返回 ok，说明本地服务起来了；日志里看到 OneBot WebSocket 已连接，才说明 QQ 链路通了。”

画面字：
- 不把 config.json/bridge.env 提交 Git
- 控制面只监听 127.0.0.1

## 26:00–31:00 DeepSeek、主人权限、睡眠和成本

画面：
- 配置文件（key/QQ 打码）。
- 私聊 bot：/状态、/缓存、/日报。
- 群聊演示：@、引用、表情（演示群打码）。
- 费用/token 图表（账号打码）。

旁白：
“DeepSeek key 用 bootstrap 写进本地配置，不要出现在视频/仓库。”
“owner 只填自己的 QQ；敏感命令只对 owner 生效。管理只走 QQ，不做网页后台。”
“睡眠、记忆、人物印象、反思这些开关都在配置里。默认 23:00–07:00 睡眠，主人私聊豁免；早上有唤醒/补回，避免半夜炸群。”
“成本方面：deepseek-flash 默认思考模式，思维链按输出计费。包里默认 low 任务关闭思考，复杂/核实保留；觉得变笨可以改回 enabled。”
“缓存只看输入，切换 thinking/重启后第一次可能整段 miss，这是正常的，不要为了命中率去写关键词黑名单。”

## 31:00–36:00 实测与翻车

画面：
- 私聊、群聊、图片、表情、拍一拍、主动消息。
- 故意展示一次失败/修复：比如 OneBot 401、端口占用、手机杀后台。
- 日志定位。

旁白：
“实测我会跑一遍：私聊、@、引用、图片、表情、拍一拍，还有主动消息。”
“也会展示翻车：OneBot token 不一致、端口冲突、手机把 VM 杀了、重启后缓存冷启动。重点不是一次成功，而是知道看哪行日志。”
“如果 QQ 不回复，按顺序查：SnowLuma 在线吗、OneBot 收到事件吗、bridge 连上 WS 吗、autoReply 开没开、是不是在睡眠/冷却/白名单。”

## 36:00–40:00 运维与隐私

画面：
- systemctl status、journalctl、/health。
- 备份目录（内容打码）。
- 隐私检查清单。

旁白：
“长期跑要会三件事：重启顺序、看日志、备份。”
“备份 config/state/memory/conversation；bridge.env 和 cookies 不要外发。备份本身也有隐私。”
“做视频/截图前，逐项打码：QQ、群、头像、二维码、API key、IP、token、路径、通知栏。用独立演示号，不要拿线上号录。”
“所有密钥如果曾经出现在文档/截图里，直接轮换，不要只删文件。”

## 40:00–42:00 结尾

画面：
- GitHub 仓库/Release 页面（链接后贴）。
- 架构图 + 手机。
- 频道关注/评论引导。

旁白：
“这就是我的来时路：从 PRoot 的坑，到一台手机里跑真 VM，再到一只会接梗、会记忆、会睡觉的 QQ 小鲸鱼。”
“包和文档会放在 GitHub，SnowLuma 请从官方渠道获取并遵守 EULA；有问题先看排错文档。”
“如果你是拿主号试的，现在关掉还来得及。小号、非商业、风险自负。”
“下期可以讲：怎么调人设、怎么控成本、怎么让它不那么像客服。拜拜。”

## 精简版剪法

- 去掉 01:30–05:00 的详细踩坑，只留 45 秒“PRoot 不行→真 VM”；
- DroidVM/SnowLuma 安装压成快剪；
- 保留成本、隐私、翻车三段；
- 目标 25 分钟。

## 附：引用与致谢（片尾字幕/简介）

- 梗库作者：B 站 **in1t_**，视频 BV1DMnS6kEkW，上游项目 in1t1a1/astrbot_plugin_memewiki、thawline/dsh-memewiki（MIT）。
- 看视频/分镜插件作者：B 站 **苍穹晓雷**，视频 BV1Hgx76SEqb，插件 13520rygvv/dsh-bili-video；上游 Yuuyuko-uu/dsh-bili-subagent、c1overzz/dsh-bili-scroll、SAOS-3624/dsh-video-storyboard。
- 本视频只做引用/致谢，不附带这些项目的源码、图片或视频资源；具体授权以作者仓库为准。
