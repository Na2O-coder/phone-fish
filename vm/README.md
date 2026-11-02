# VM 侧文件

- systemd/snowluma-bridge.service：bridge 的 unit 模板；
- systemd/snowluma.service.example：仅参考，SnowLuma 的安装/启动按官方文档；
- setup-vm.sh：通用 Ubuntu 初始化（SSH/时区/禁休眠/基础依赖），不安装 SnowLuma；
- ports-and-network.md：端口和安全边界。

SnowLuma rootfs、二进制、Docker 镜像不在本仓库；请阅读 docs/03。
