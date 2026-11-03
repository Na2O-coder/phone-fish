# Changelog

## Unreleased

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
