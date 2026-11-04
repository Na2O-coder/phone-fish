# phone-fish：在安卓手机上运行 DeepSeek QQ AI 小号

> SnowLuma + OneBot + DeepSeek bridge · 硬件虚拟化优先 · 软件模拟实验性 · Agent 辅助部署

phone-fish 是一个尝试把 **QQ AI 小号完整部署在安卓手机本地 Linux 环境**里的开源项目。

**一句话定位：面向安卓手机的低开销虚拟化 QQ AI 自托管方案。**

它不跟 AstrBot + NapCat 比“谁部署更简单”，而是尝试解决另一个问题：**QQ 接入路线怎么选，虚拟机怎么跑，以及怎么让 Agent 按文档协助部署而不是瞎装。**

项目提供两条虚拟机运行思路：

- **硬件虚拟化（推荐：Gunyah / GenieZone / KVM）**：需要 Root 和可用的硬件虚拟化后端，性能开销通常较低；
- **软件模拟虚拟机（Experimental / 实验性）**：不依赖硬件虚拟化，但性能、发热和耗电可能明显增加，目前尚未完成端到端实测。

> 本项目不是 QQ 官方产品，也不隶属于腾讯、DeepSeek、SnowLuma 或 DroidVM。任何第三方 QQ 接入方案都不能保证账号永久安全。

## 项目定位

市面上的 QQ 机器人方案很多。AstrBot + NapCat 这类方案通常更轻、部署门槛更低，但 NapCat 属于第三方协议端，仍然存在平台风控和账号限制风险。

phone-fish 选择的是另一条 QQ 接入路线：

~~~text
QQ 小号
→ SnowLuma（Linux QQ 客户端路线）
→ OneBot v11
→ phone-fish bridge
→ DeepSeek API
~~~

这样做的目的，是**尝试降低协议端带来的账号风险**，而不是承诺“不会封号”。SnowLuma 本身也有独立 EULA，任何第三方 QQ 客户端/自动化工具都应该只用小号，并自行评估风险。

## 两条虚拟机路线

| | 路线 A：硬件虚拟化 | 路线 B：软件模拟 |
|---|---|---|
| 推荐度 | 推荐 | Experimental / 实验性 |
| Root | 需要 | 通常不需要 |
| 硬件要求 | 需要可用的 Gunyah / GenieZone / KVM 后端 | 对虚拟化支持依赖较低 |
| 性能开销 | 通常较低 | 通常较高 |
| 发热/耗电 | 取决于实际负载 | 相同负载下可能更高 |
| 适合 | 长期运行、备用机/主力机 | 没有硬件虚拟化时尝试，或做兼容性验证 |

**处理器型号不等于支持硬件虚拟化。** 骁龙 8 Gen 3 / 8 Elite、天玑旗舰只是“可以优先检查”，最终取决于厂商内核、固件、虚拟化接口，以及 DroidVM 能否实际启动 VM。

路线选择、检测命令和性能测试方法见：

- [00-选择虚拟机运行路线](docs/00-选择虚拟机运行路线.md)
- [12-设备兼容性检测](docs/12-设备兼容性检测.md)
- [13-性能与功耗测试](docs/13-性能与功耗测试.md)

## 和 AstrBot + NapCat 的区别

| 对比项 | AstrBot + NapCat | phone-fish + SnowLuma |
|---|---|---|
| 核心架构 | AstrBot + NapCat | SnowLuma + OneBot + bridge |
| QQ 接入 | NapCat（第三方协议端） | SnowLuma（Linux QQ 客户端路线） |
| AI 模型 | DeepSeek 等 | DeepSeek |
| 手机部署 | 有社区方案 | 本项目主要目标 |
| Root | 视部署方案而定 | 硬件虚拟化路线需要 |
| 虚拟机 | 通常不需要 | 硬件虚拟化或软件模拟 |
| 运行开销 | 取决于部署 | 硬件虚拟化通常较低；软件模拟较高 |
| 账号风险 | 存在 | 仍存在，无法保证不封号 |
| 部署难度 | 通常较低 | 较高 |
| Agent 部署引导 | 取决于项目 | 仓库内置 AGENTS.md + 专门手册 |

这张表只说明取舍，不把未经统计验证的账号风险差异包装成“客观保证”。

### 先分清：框架和 QQ 接入端是两层

AstrBot 是 QQ bot 框架，NapCat / SnowLuma 是 OneBot 接入端，两者可以组合。上表用「AstrBot + NapCat」代表常见组合；「AstrBot + SnowLuma」理论上也可行，但需要实测 SnowLuma 的 OneBot 兼容性。

phone-fish 对比的重点不是「框架 vs 接入端」，而是「通用框架 + 协议端」与「定制 bridge + SnowLuma」这两种整体方案。

### 能不能换成 AstrBot + SnowLuma？

技术上可以：保留 SnowLuma 作为 OneBot 接入端，把 phone-fish bridge 换成 AstrBot。AstrBot 官方支持 OneBot v11，但 SnowLuma 的事件、图片、语音、表情和引用字段兼容性需要实测。

注意：AstrBot 是 AGPL-3.0，本仓库 bridge/文档是 MIT；不要把 AstrBot 代码直接并进 bridge。替换会得到插件生态、WebUI、多平台和知识库等能力，但会失去当前定制的人格、关系/心情、睡眠、群规则和表情逻辑，需要迁移。QQ 接入仍然是 SnowLuma，账号风险不会因为换框架而消失。

## 架构

### 组件层次

| 组件 | 职责 | 所在层 |
|---|---|---|
| DroidVM | 创建、管理、运行虚拟机 | Android 宿主机 |
| Gunyah / GenieZone / KVM | 硬件虚拟化后端 | Android / Linux 内核 |
| Ubuntu | 提供 Linux 运行环境 | 虚拟机内部 |
| SnowLuma | QQ 客户端、WebUI、OneBot | Ubuntu 内的 chroot |
| OneBot | 提供 QQ 消息接口 | SnowLuma |
| snowluma-bridge | DeepSeek 接入、消息处理 | Ubuntu |
| DeepSeek API | 提供大模型能力 | 外部服务 |

### 硬件虚拟化说明

本项目演示环境使用高通骁龙设备，由 Qualcomm Gunyah 提供硬件虚拟化能力。**DroidVM 是 VM 管理器，不是 Hypervisor 本身。**

DroidVM 可用的硬件虚拟化后端取决于设备：

- Qualcomm Gunyah：`/dev/gunyah`
- MediaTek GenieZone：`/dev/gzvm`
- Linux KVM：`/dev/kvm`

具体使用哪一种，取决于手机 SoC、固件和内核环境。**设备节点存在只说明接口出现，不等于 VM 一定能启动**；不要仅凭处理器型号判断。

### 部署结构（演示环境：Gunyah + DroidVM）

~~~text
Android 宿主机
└── DroidVM（VM 管理器）
    ├── Gunyah / GenieZone / KVM（硬件虚拟化后端）
    └── Ubuntu VM
        ├── SnowLuma（官方 rootfs / chroot）
        │   ├── WebUI       :5099
        │   ├── OneBot HTTP :3000
        │   └── OneBot WS   :3001
        └── snowluma-bridge（本项目）
            └── DeepSeek API
~~~

> 软件模拟路线是实验性备选：上层 Ubuntu / SnowLuma / bridge 的目标一致，但 VM 工具、性能和功耗代价不同；见 [00-选择虚拟机运行路线](docs/00-选择虚拟机运行路线.md)。

**Docker 不在运行路径里。** 获取镜像时可以用 OCI/Docker 格式相关工具（skopeo/umoci）解包 rootfs；实际运行是 chroot，不需要 Docker Engine。

## 项目特点

- **不需要电脑**：主要运行环境部署在安卓手机内部；
- **不需要常开云服务器**：Linux、SnowLuma 和 bridge 都在手机/虚拟机里；
- **SnowLuma 接入路线**：使用 Linux QQ 客户端 + OneBot，而不是 NapCat 协议端；
- **双虚拟机路线**：硬件虚拟化优先（Gunyah / GenieZone / KVM），软件模拟作为实验性备选；
- **长期记忆/关系/表情/睡眠/权限**：bridge 侧的可配置行为；
- **Agent 辅助部署**：AGENTS.md + docs/14，让 DSH 或其他 Agent 先检查环境、再分阶段执行；
- **隐私优先**：仓库不包含密钥、QQ 数据、聊天记录、rootfs 或 APK。

## 先读这些

1. [选择虚拟机运行路线](docs/00-选择虚拟机运行路线.md)
2. [架构与原理](docs/01-架构与原理.md)
3. [手机与 DroidVM 准备](docs/02-手机与DroidVM准备.md)
4. [虚拟机初始化](docs/03-虚拟机初始化.md)
5. [SnowLuma 安装与授权说明](docs/04-SnowLuma安装与授权说明.md)
6. [OneBot 与 QQ 登录](docs/05-OneBot与QQ登录.md)
7. [bridge 安装与配置](docs/06-bridge安装与配置.md)
8. [DeepSeek 与主人权限](docs/07-DeepSeek与主人权限.md)
9. [运维、备份与排错](docs/08-运维备份与排错.md)
10. [新机上手检查表](docs/09-新机上手检查表.md)
11. [发布与维护](docs/10-发布与维护.md)
12. [手机-only 获取 SnowLuma 镜像](docs/11-SnowLuma镜像获取-DockerTarBuilder.md)
13. [设备兼容性检测](docs/12-设备兼容性检测.md)
14. [性能与功耗测试](docs/13-性能与功耗测试.md)
15. [给 DSH/Agent 的安装调试手册](docs/14-给DSH或Agent的安装调试手册.md)

## 重要授权说明

**SnowLuma 不是本仓库的代码，也不随本仓库分发。**

SnowLuma 的 EULA 为源码可见、非商业许可，并限制将官方发行包/专有组件并入第三方安装包、Docker 镜像，或通过自动化脚本部署；公开发布修改/衍生物也需要书面授权。请先阅读官方 EULA 和官方安装文档，并自行取得软件。

通过 GitHub Actions 在自己的账号里拉取官方镜像，也必须自行确认是否符合当前 EULA；不要因为“是用户自己操作”就默认整个流程合规。

本仓库只包含：

- 我编写/整理的 bridge 与文档；
- 通用配置模板、构建工具和隐私扫描工具；
- 排错笔记和 Agent 部署手册。

本仓库**不包含**：

- SnowLuma rootfs/二进制/Docker 镜像；
- DroidVM APK；
- 任何 QQ 账号、群号、API key、cookie、聊天记录、记忆数据库；
- 真实个人人设或私人关系文本。

## 快速开始（简版）

0. **先选路线**：读 [00-选择虚拟机运行路线](docs/00-选择虚拟机运行路线.md)，按 [12-设备兼容性检测](docs/12-设备兼容性检测.md) 做只读检查。
1. **硬件虚拟化路线**：准备 Root 安卓手机、DroidVM、兼容的硬件虚拟化后端；软件模拟路线目前是实验性，不要当作已验证方案。
2. 按 [02-手机与DroidVM准备](docs/02-手机与DroidVM准备.md) 和 [03-虚拟机初始化](docs/03-虚拟机初始化.md) 建 Ubuntu VM。
3. **按 SnowLuma 官方文档**安装并登录，开启 OneBot WS 3001；镜像获取见 [11-SnowLuma镜像获取-DockerTarBuilder](docs/11-SnowLuma镜像获取-DockerTarBuilder.md)。
4. 把本包解压到 VM，例如 /opt/snowluma/phone-fish，生成配置并安装 bridge：

~~~bash
cd /opt/snowluma/phone-fish
python3 tools/bootstrap.py
sudo cp -a bridge/. /opt/snowluma/bridge/
sudo cp vm/systemd/snowluma-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now snowluma-bridge
~~~

5. 按 [05-OneBot 与 QQ 登录](docs/05-OneBot与QQ登录.md) 和 [07-DeepSeek 与主人权限](docs/07-DeepSeek与主人权限.md) 配置 OneBot、DeepSeek key 和 owner。
6. 私聊 bot 测试，按 [08-运维、备份与排错](docs/08-运维备份与排错.md) 排查。

> 包内路径可能按你的解压位置调整；完整步骤以 docs 为准。

## 性能与功耗

硬件虚拟化的“低开销”是相对软件模拟而言，不是零开销。内存、设备模拟、I/O 和后台运行仍然有成本。

| 项目 | 硬件虚拟化 | 软件模拟 |
|---|---|---|
| CPU 执行 | 硬件辅助虚拟化 | 软件模拟 |
| 性能开销 | 通常较低 | 通常较高 |
| 额外发热 | 取决于实际负载 | 同负载下更容易增加 |
| 额外耗电 | 通常较少 | 同负载下可能更高 |
| 长期运行 | 推荐 | 视设备能力而定，实验性 |

目前没有统一的实测数据；测试方法和记录表见 [13-性能与功耗测试](docs/13-性能与功耗测试.md)。视频里如果要讲“省电/低开销”，建议先做一次真机对比，不要只讲理论。

## 安全与隐私

- 只用 QQ 小号，不要接主号；
- 控制 API 只监听 127.0.0.1；
- `.env`、`config.json`、`state.json`、`memory.db`、日志不要发人、不要提交；
- 发布截图/视频前必须打码 QQ、群、API key、IP、token、聊天内容；
- SnowLuma 和 DroidVM 的获取、安装、授权由你自己完成；
- 详细清单见 [PRIVACY.md](PRIVACY.md)。

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

本项目仅供个人学习、研究和非商业自托管。使用非官方 QQ 客户端或自动化工具可能违反平台条款并导致账号风险；请自行评估并遵守当地法律与平台规则。作者不对封号、数据丢失或任何间接损失负责。

## 引用与致谢

梗库、看视频/分镜能力的作者与上游项目见 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)。特别感谢 B 站作者 in1t_（梗库）和 苍穹晓雷（看视频/分镜插件），以及他们声明的 MIT 上游项目。

## 官方项目（仅链接，不是我的项目）

- **SnowLuma**
  - 项目：https://github.com/SnowLuma/SnowLuma
  - 文档：https://snowluma.github.io/
  - EULA：https://github.com/SnowLuma/SnowLuma/blob/main/EULA.md
- **DroidVM**
  - 项目/官方获取入口：https://github.com/droidvm/app

SnowLuma 和 DroidVM 都不是我的项目，本仓库只放官方链接，不打包、不镜像、不代分发它们的 APK、rootfs 或二进制。链接和教程不构成任何官方授权或背书；下载和使用请遵守各自许可证/EULA，SnowLuma 尤其注意非商业和再分发限制。

## 表情包致谢

当前实例的表情包接入感谢 B 站作者 **可以叫我云大人 / 雾雪**（项目 [Loping151/dsh-nekowhale-stickers](https://github.com/Loping151/dsh-nekowhale-stickers)，视频 BV13xnt6QE6s）。表情图片/目录不随本仓库分发；许可与使用方式见作者项目。
