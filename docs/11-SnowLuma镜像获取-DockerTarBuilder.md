# 11 · 手机-only 获取 SnowLuma 镜像（DockerTarBuilder）

> 目标：没有电脑、没有本地 Docker，也能拿到 SnowLuma 的 arm64 Docker 镜像压缩包。
>
> 重要：这只是让你自己的 GitHub Actions 去官方镜像仓拉取并打包，不是把 SnowLuma 再分发给别人。SnowLuma 的 EULA 仍然约束你对镜像/rootfs 的使用；不要把生成的 tar 上传到公开 Release、网盘或群里。

## 0. 原理

DockerTarBuilder 是一个 GitHub Actions 工作流：

1. 你在自己的 GitHub 账号里 fork 它；
2. 手动运行 ARM64 Pull and Save Docker Image；
3. GitHub 的 runner 执行 docker pull motricseven7/snowluma:<tag> --platform linux/arm64；
4. 用 docker save 导出并 gzip；
5. 你从自己仓库的 Actions Artifact 下载 .tar.gz。

整个过程不需要你的手机跑 Docker，也不需要电脑；镜像来自官方 Docker Hub，不经过本文档作者分发。

## 1. 准备

- GitHub 账号（手机浏览器即可）；
- 已经 fork 的 DockerTarBuilder（用户自己的 fork）；
- 手机上有能解压 zip 的工具（ZArchiver/系统文件管理器）；
- VM 有 5 GB 以上空闲磁盘；
- 已确认要用的 SnowLuma 镜像 tag。

## 2. fork（如果还没有）

1. 手机浏览器打开 https://github.com/wukongdaily/DockerTarBuilder
2. 点右上角 Fork → 选自己的账号；
3. 进入自己的 fork，例如：

~~~text
https://github.com/<你的用户名>/DockerTarBuilder
~~~

> 不要直接在上游仓库跑；用自己的 fork，artifact 只存在你的账号下。

## 3. 运行工作流

1. 打开自己 fork 的 Actions 标签；
2. 如果提示 Actions 未启用，点启用（只启用这个仓库的 workflow）；
3. 左侧选 ARM64 Pull and Save Docker Image（Artifact 工作流）；
4. 右侧 Run workflow；
5. 在 docker_images 填：

~~~text
motricseven7/snowluma:v2.1.1
~~~

如果上游已经更新，换成你要的 tag；多镜像用英文逗号分隔。

6. 点 Run workflow，等 3–10 分钟。

> 镜像约 1.8–2 GB，GitHub 免费额度/网络可能较慢；失败就重跑。
>
> 另一个 Get-ARM64-Docker-Images-Release 工作流会把 tar 放到你的 Release；如果镜像小于 2 GB 可以用，但公开 Release 会变成再分发，更推荐 Artifact，并在下载后删除。

## 4. 下载 Artifact

1. 进入这次 workflow run；
2. 页面底部 Artifacts → 下载 my-artifact.zip；
3. 手机解压得到类似：

~~~text
motricseven7_snowluma_v2.1.1-arm64.tar.gz
~~~

4. 用共享目录/scp 传到 VM，例如：

~~~bash
scp -P 2222 motricseven7_snowluma_v2.1.1-arm64.tar.gz <用户>@127.0.0.1:/tmp/
~~~

没有 scp 就先用手机文件管理器放到 DroidVM 共享目录，再在 VM 里 cp。

## 5. 从 tar 得到 rootfs（VM 内，不需要 Docker）

VM 里不要装 Docker。用 skopeo + umoci 解包：

~~~bash
sudo apt-get update
sudo apt-get install -y skopeo umoci
cd /tmp
gzip -dc motricseven7_snowluma_v2.1.1-arm64.tar.gz > snowluma-image.tar
skopeo copy docker-archive:/tmp/snowluma-image.tar oci:/tmp/snowluma-oci:v2.1.1
umoci unpack --image /tmp/snowluma-oci:v2.1.1 /tmp/snowluma-unpack
ls -ld /tmp/snowluma-unpack/rootfs
~~~

如果 umoci 报镜像引用格式错误，先检查：

~~~bash
skopeo inspect docker-archive:/tmp/snowluma-image.tar | head
~~~

然后把 rootfs 放到运行目录（全新安装，不要覆盖正在用的实例）：

~~~bash
sudo mv /opt/snowluma-root /opt/snowluma-root.bak.$(date +%s) 2>/dev/null || true
sudo cp -a /tmp/snowluma-unpack/rootfs /opt/snowluma-root
sudo mkdir -p /opt/snowluma/data /opt/snowluma/config /opt/snowluma/local-share
~~~

> 如果你的 rootfs 结构不同（有的镜像 rootfs 直接在 /tmp/snowluma-unpack），按实际 ls 结果调整。不要照抄到覆盖线上实例。

## 6. 清理

~~~bash
rm -f /tmp/snowluma-image.tar /tmp/*.tar.gz
rm -rf /tmp/snowluma-oci /tmp/snowluma-unpack
~~~

- 下载后可在 GitHub Actions 页面删除这次 run/artifact；
- 不要把这个 tar 上传到公开 Release、网盘、群文件；
- 不要把镜像 tar/rootfs 提交进 Git。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| Actions 不能运行 | 在自己 fork 的 Actions 页启用；fork 的 workflow 可能要先点 I understand my workflows |
| 拉取失败 | 换 tag；重跑；GitHub runner 网络波动 |
| artifact 下载很慢 | 用 GitHub 网页/手机网络重试；不要找第三方代下 |
| tar 解压报错 | 下载不完整；重新下载；检查 gzip -t |
| skopeo 报 manifest | 确认是 arm64；确认 tag 存在；skopeo inspect |
| 空间不足 | 删旧 rootfs/日志/备份；VM 磁盘至少 25 GB |
| 想用 Docker load | VM 内核没有 Docker，别装 dockerd；用 skopeo/umoci |

## 8. 授权提醒

- 该做法只解决“我自己取官方镜像”；
- 不代表 SnowLuma 授权你公开再分发、公开自动化部署或发布衍生镜像；
- 公开视频/仓库不要附带 tar/rootfs；
- 如果 SnowLuma 作者提出要求，停止公开分发并沟通。
