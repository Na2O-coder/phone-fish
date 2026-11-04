# 02 · 手机与 DroidVM 准备（硬件虚拟化路线）

## 1. 现实门槛

本文是 **硬件虚拟化路线**。没有 Root 或没有可用硬件虚拟化后端的设备，先读 [00-选择虚拟机运行路线](00-选择虚拟机运行路线.md)；软件模拟路线目前是实验性，不要当作已验证方案。

这不是“所有安卓手机都能跑”的教程。先确认：

- Android 13 或更高；
- 已 root（Magisk / KernelSU / APatch 等）；
- 手机 SoC/内核支持硬件虚拟化：
  - 高通 Gunyah（骁龙 8 Gen 3 或更新，具体看官方）；
  - 联发科 GenieZone（天玑 9000 或更新）；
  - 或设备以 EL2 启动并启用 Linux KVM；
- 至少 8 GB RAM，建议 12 GB；
- 至少 20 GB 可用存储（VM 磁盘 25 GB，rootfs 约 3 GB）；
- 一台全新的 QQ 小号；
- 一个 DeepSeek API key（有余额）。

> 不满足就别硬上。没有 root/虚拟化节点的手机跑不了硬件虚拟化路线；可以评估软件模拟，但性能和续航可能明显变差。

## 2. 硬件虚拟化平台说明

**DroidVM 是 VM 管理器/运行环境，不是 Hypervisor 本身。** 硬件虚拟化能力由手机 SoC、内核和固件提供的后端完成。

常见后端与设备节点：

| 平台 | 常见设备节点 | 说明 |
|---|---|---|
| Qualcomm Gunyah | `/dev/gunyah` | 本项目演示环境使用的高通方案 |
| MediaTek GenieZone | `/dev/gzvm` | 联发科设备的硬件虚拟化后端 |
| Linux KVM | `/dev/kvm` | Linux 内核虚拟化接口 |

在手机 Root Shell 里做只读检查：

~~~bash
su
ls -l /dev/gunyah /dev/gzvm /dev/kvm 2>/dev/null
uname -a
getprop ro.product.model
getprop ro.board.platform
getprop ro.build.version.release
~~~

注意：

- 设备节点存在只说明接口出现，不代表 DroidVM 一定能启动 VM；必须实际启动一个最小 VM 验证。
- 不要只根据 SoC 型号判断支持哪种后端。
- 本项目演示截图和流程以 **Qualcomm Gunyah + DroidVM** 为准；其他设备按实际后端识别，不要照搬。

## 3. 安装 DroidVM

DroidVM 官方 APK 是闭源软件（包名 com.zzvm），本仓库不打包、不提供镜像。

请从官方渠道获取：

- 官方项目/发布：https://github.com/droidvm/app
- 具体安装、root 授权、虚拟化支持以官方说明为准。

安装后授予 root 权限，打开 App，确认能看到虚拟化后端（Gunyah/GenieZone/KVM）。

## 4. 创建 Ubuntu VM

建议参数（按手机内存调整）：

| 项 | 建议 |
|---|---|
| 系统 | Ubuntu 24.04/26.04 ARM64 |
| CPU | 2–4 vCPU |
| 内存 | 3–4 GB（手机 8 GB 用 3 GB，12 GB 用 4 GB） |
| 磁盘 | 25 GB |
| 网络 | 默认 NAT / IPv4 |
| 显示 | 默认 VNC |

> 4 GB VM 起不来时，先用 3 GB 把系统装好，再尝试加大；不要同时开一堆后台应用。

## 5. 端口转发

在 DroidVM 的 NIC 设置里添加：

~~~text
tcp 2222:22
tcp 5099:5099
tcp 3000:3000
tcp 3001:3001
tcp 6081:6081
~~~

作用：

- 2222：从手机 SSH 进 VM；
- 5099：SnowLuma WebUI；
- 3000/3001：OneBot HTTP/WS（bridge 在 VM 内访问 127.0.0.1:3001，通常不需要从手机外访问）；
- 6081：noVNC，用来看 QQ 登录/扫码画面。

## 6. 共享目录（可选）

如果 DroidVM 支持 virtiofs/9p，可以把手机目录挂到 VM：

~~~bash
sudo mkdir -p /mnt/phone
sudo mount -t virtiofs shared /mnt/phone
# 或
sudo mount -t 9p -o trans=virtio shared /mnt/phone
~~~

然后可以把本包/截图/安装包从手机传到 VM。共享目录失败也不会影响运行，可以改用 scp。

## 7. 手机端操作方式

没有电脑也可以：

- 用 DroidVM 自带的 VNC/终端进入 VM；
- 或在手机装一个 SSH 客户端，连 127.0.0.1:2222；
- 所有命令都在 VM 内执行，不需要 adb/PC。

## 8. 风险提示

- 不要用 QQ 主号；
- 新设备/新 IP 首次登录容易触发验证码；
- 不要频繁重建容器/切换网络；
- 不要同时用官方 QQ 客户端登录同一个小号；
- 使用非官方协议工具可能违反 QQ 条款，风险自负。
