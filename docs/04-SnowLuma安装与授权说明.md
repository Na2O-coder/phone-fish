# 04 · SnowLuma 安装与授权说明

## 重要：先读授权

SnowLuma 不是本仓库的软件，也不随本仓库分发。

- 官方文档：https://snowluma.github.io/
- 源码与发行：https://github.com/SnowLuma/SnowLuma
- EULA：https://github.com/SnowLuma/SnowLuma/blob/main/EULA.md

SnowLuma 的 EULA 为「源码可见、非商业」许可，并限制：

- 将官方发行包/专有组件并入第三方安装包或 Docker 镜像；
- 通过自动化脚本部署；
- 公开发布修改版/衍生版。

以上通常需要著作权人的书面授权。**本仓库因此不提供 SnowLuma rootfs、二进制、镜像或自动部署脚本。**

如果你要公开做视频/教程，建议先向 SnowLumaDevs 说明用途并取得书面许可；如果对方不同意，视频只保留架构/踩坑讲解，不演示自动安装。

## 推荐安装顺序（以官方文档为准）

1. 在 VM 里准备官方要求的运行环境；
2. 从官方渠道获取 SnowLuma（官方 Docker 镜像/发行包/文档指定的方式）；
3. 启动 WebUI（默认 5099）和 OneBot；
4. 打开 WebUI 的二维码，用 **QQ 小号** 扫码登录；
5. 在 OneBot 设置中开启 WebSocket 3001，设置随机 token；
6. 确认 VM 内 curl 能访问 127.0.0.1:3001。

> 本仓库不提供具体 rootfs 解包脚本，也不保证你的内核/Ubuntu 版本与作者环境一致。请以 SnowLuma 官方安装文档为准。

## 本项目的 chroot 路线是什么

作者的实践是：

- 从官方 Docker 镜像 rootfs 中取文件（不是重新打包/再分发镜像）；
- 在 VM 里用 chroot 运行官方 rootfs；
- 绑定 /proc、/sys、/dev、/dev/pts、/dev/shm 和可写数据目录；
- 用 systemd 管理 SnowLuma 的启动。

这只是作者自己的运行经验，不是 SnowLuma 官方支持的安装方式。**不要把它当成官方安装脚本发布。**

## 需要记录的信息

安装完成后确认：

| 项 | 默认/示例 |
|---|---|
| WebUI | http://127.0.0.1:5099 |
| OneBot HTTP | 127.0.0.1:3000 |
| OneBot WS | ws://127.0.0.1:3001 |
| OneBot token | 你自己设置，写入 bridge 配置 |
| QQ 小号 | 只用于这个 bot，不要用主号 |

## 常见问题

- WebUI 打不开：端口转发/VM 防火墙/服务未启动；
- 扫码后掉线：不要同时用官方 QQ 客户端登录同号；网络要稳定；
- OneBot 401：bridge 的 onebotToken 与 SnowLuma 设置不一致；
- 容器/rootfs 起不来：确认是官方 rootfs、架构是 arm64、内核支持所需能力；
- 授权问题：直接联系 SnowLumaDevs，不要绕过 EULA 分发。

## 官方链接

- SnowLuma 手机部署文档：https://snowluma.github.io/zh/docs/guide/deploy/mobile
- Docker 部署文档：https://snowluma.github.io/zh/docs/guide/deploy/docker
- SnowLuma 仓库：https://github.com/SnowLuma/SnowLuma
