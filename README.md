# 手机养鱼：纯安卓手机跑 QQ AI 小号

> DroidVM 真虚拟机 + SnowLuma(chroot) + OneBot + DeepSeek bridge

这是一个「没有电脑、没有常开服务器，只有一台安卓手机」的 QQ AI 陪伴小号实践包。
它记录了我从 Operit/PRoot/udocker 的失败路线，最终走到 **DroidVM 真虚拟机 + SnowLuma** 的来时路，并把可以公开的部分整理成可复现的配置、bridge、文档和视频脚本。

## 这是什么

~~~text
Android 手机（root，DroidVM）
└── Ubuntu VM（2–4 vCPU / 3–4 GiB / 25 GB）
    ├── SnowLuma（chroot，跑官方 rootfs）
    │   ├── WebUI  :5099
    │   ├── OneBot HTTP :3000
    │   └── OneBot WS   :3001
    └── snowluma-bridge（本项目）
        └── DeepSeek API
~~~

- **不是**电脑教程的搬运：整条链路都在安卓手机上；
- **不是**一键傻瓜包：DroidVM、SnowLuma、QQ 登录、API key 都需要你自己完成；
- **不是**官方项目：与 SnowLuma、DroidVM、腾讯/QQ、DeepSeek 均无隶属关系。

## 先读这些

1. [架构与原理](docs/00-架构与原理.md)
2. [手机与 DroidVM 准备](docs/01-手机与DroidVM准备.md)
3. [虚拟机初始化](docs/02-虚拟机初始化.md)
4. [SnowLuma 安装与授权说明](docs/03-SnowLuma安装与授权说明.md)
5. [OneBot 与 QQ 登录](docs/04-OneBot与QQ登录.md)
6. [bridge 安装与配置](docs/05-bridge安装与配置.md)
7. [DeepSeek 与主人权限](docs/06-DeepSeek与主人权限.md)
8. [运维、备份与排错](docs/07-运维备份与排错.md)
9. [新机上手检查表](docs/08-新机上手检查表.md)
10. [给 DSH/Agent 的安装调试手册](docs/11-给DSH或Agent的安装调试手册.md)
11. [手机-only 获取 SnowLuma 镜像](docs/10-SnowLuma镜像获取-DockerTarBuilder.md)
12. [B 站视频脚本套件](docs/video/script.md)

## 重要授权说明

**SnowLuma 不是本仓库的代码，也不随本仓库分发。**

SnowLuma 的 EULA 为源码可见、非商业许可，并限制将官方发行包/专有组件并入第三方安装包、Docker 镜像，或通过自动化脚本部署；公开发布修改/衍生物也需要书面授权。请先阅读官方 EULA 和官方安装文档，并自行取得软件。

本仓库只包含：

- 我编写/整理的 bridge 与文档；
- 通用配置模板和隐私扫描/构建工具；
- 视频脚本和排错笔记。

本仓库**不包含**：

- SnowLuma rootfs/二进制/Docker 镜像；
- DroidVM APK；
- 任何 QQ 账号、群号、API key、cookie、聊天记录、记忆数据库；
- 真实个人人设或私人关系文本。

如果 SnowLuma 的授权要求与你的使用方式冲突，请停止使用并联系其作者。

## 快速开始（简版）

1. 准备 root 安卓手机、DroidVM、QQ 小号、DeepSeek key；
2. 建 Ubuntu VM，按 [虚拟机初始化](docs/02-虚拟机初始化.md) 做 SSH/时区/禁休眠；
3. **按 SnowLuma 官方文档**安装并登录，开启 OneBot WS 3001；
4. 把本包解压到 /opt/snowluma/phone-fish，生成配置并复制到运行目录：

~~~bash
sudo chown -R "$USER:$USER" /opt/snowluma/phone-fish
cd /opt/snowluma/phone-fish
python3 tools/bootstrap.py
sudo cp -a bridge/. /opt/snowluma/bridge/
sudo cp vm/systemd/snowluma-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now snowluma-bridge
~~~

5. 私聊 bot 测试，按 [DeepSeek 与主人权限](docs/06-DeepSeek与主人权限.md) 设置 owner；
6. 看 [排错](docs/07-运维备份与排错.md)。

> 包内路径可能按你的解压位置调整；完整步骤以 docs 为准。

## 安全与隐私

- 只用 QQ 小号，不要接主号；
- 控制 API 只监听 127.0.0.1；
- .env、config.json、state.json、memory.db、日志不要发人、不要提交；
- 发布截图/视频前必须打码 QQ、群、API key、IP、token、聊天内容；
- 详细清单见 [PRIVACY.md](PRIVACY.md) 和 [视频隐私检查表](docs/video/privacy-checklist.md)。

## 开发/验证

~~~bash
python3 tests/bridge_v2/run_all.py
sh tools/smoke_test.sh
python3 tools/redact_check.py .
python3 tools/build_release.py
~~~

## License

- 本项目自己的 bridge、工具和文档：MIT，见 [LICENSE](LICENSE)；
- 第三方组件与数据：见 [NOTICE.md](NOTICE.md)；
- SnowLuma、DroidVM、QQ/腾讯、DeepSeek 各有自己的条款，不在本项目 MIT 范围内。

## 免责声明

本项目仅供个人学习、研究和非商业自托管。使用非官方 QQ 协议工具可能违反平台条款并导致账号风险；请自行评估并遵守当地法律与平台规则。作者不对封号、数据丢失或任何间接损失负责。
