# Changelog

## Unreleased

- 新增每天早上的主动消息（morningGreeting）：固定时间向配置群发一条短消息，不引用昨晚聊天；支持 /早安 on|off|status 按群开关。
- 强化 Agent 辅助部署定位：AGENTS.md 增加 dshgo/DSH 启动提示词、部署后用户应交给人设/LLM/TTS/STT/账号/群配置清单和 secret 处理规则。
- 关闭旧的睡眠“早晨补回”：夜间消息静默，不再在早上引用昨晚消息生成补回。
- 新增存储只读保护：启动检查根目录，state/DB 写失败给主人告警，EROFS 时停止重试并提示 fsck；/health 暴露 storage 状态。
- 补充硬件虚拟化平台说明：Qualcomm Gunyah（`/dev/gunyah`）、MediaTek GenieZone（`/dev/gzvm`）、Linux KVM（`/dev/kvm`）。
- 明确 DroidVM 是 VM 管理器，不是 Hypervisor；KVM 是硬件虚拟化后端，不是独立 VM 软件。
- README / AGENTS / docs/00 / 02 / 09 / 11 / 12 / 13 / 14 统一硬件虚拟化与软件模拟的术语和预检命令。
- 澄清 OCI/Docker 镜像解包只用于提取 rootfs，运行时是 chroot，不依赖 Docker Engine。

## 0.2

发布时间：2026-11-03

- README 重新定位为「安卓手机上的低开销虚拟化 QQ AI 自托管方案」，补充 KVM / 软件模拟双路线。
- 新增 docs/00 路线选择、docs/12 设备兼容性检测、docs/13 性能与功耗测试。
- docs 重新编号：00 路线，01 架构，02–09 部署，10 发布，11 镜像，12 兼容性，13 性能，14 Agent 手册。
- AGENTS.md 增加虚拟化路线判断、SnowLuma 授权边界、操作分级和环境检测报告。
- 公开包只包含代码、文档和模板；视频素材只保留在本地。

## 0.1.0

- 首次整理「手机养鱼」公开包：
  - 脱敏 bridge（OneBot WS ↔ DeepSeek）；
  - 新机使用指南、VM/运维文档；
  - 通用 bootstrap、隐私扫描、打包、smoke test；
  - 可选 MIT 梗百科数据库 release asset。
- 不包含 SnowLuma 二进制/rootfs、DroidVM APK、个人数据。
