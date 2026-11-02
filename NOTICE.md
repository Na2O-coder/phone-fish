# 第三方组件与授权说明

本项目自己的 bridge、工具和文档使用 MIT（见 LICENSE）。以下内容不属于本项目，且不随本仓库分发。

## SnowLuma

- 官网/文档：https://snowluma.github.io/
- 源码/发行：https://github.com/SnowLuma/SnowLuma
- EULA：https://github.com/SnowLuma/SnowLuma/blob/main/EULA.md
- 许可：SnowLuma Source-Available Non-Commercial License + EULA；官方发行包含专有组件。
- 本仓库不包含 SnowLuma 的 rootfs、二进制、Docker 镜像或专有组件，也不提供自动部署脚本。请从官方渠道获取并遵守其 EULA；公开发布衍生/自动化部署前需取得作者书面授权。

## DroidVM

- 官方项目：https://github.com/droidvm/app
- 官方 APK 为闭源软件（包名 com.zzvm，柳州正卓软件有限公司），本仓库不打包、不镜像分发 APK。
- 请从官方渠道安装；不同手机 SoC/内核的支持情况以官方说明为准。

## 梗百科数据

- 上游：https://github.com/in1t1a1/astrbot_plugin_memewiki
- 本包使用的整理版本：https://github.com/thawline/dsh-memewiki
- 许可：MIT；数据以 release asset 形式提供，随附 LICENSE 和来源说明。表情图片不随包分发。

## DeepSeek

- API 文档：https://api-docs.deepseek.com/
- 使用需要你自己的 API key 和余额；本项目不提供 key，也不承担费用。

## QQ / 腾讯

- QQ 是腾讯的商标/产品；本项目与腾讯无隶属、合作或授权关系。
- 非官方协议工具可能违反《QQ 用户协议》并导致账号风险；请只用小号，自行承担风险。

## OneBot / SnowLuma 生态

- OneBot v11 是接口约定；具体实现由 SnowLuma/其依赖提供，不在本项目 MIT 范围内。

## 表情目录/图片

本仓库不包含他人表情包图片或表情目录数据；如需使用，请自行确认授权并自行导入。

## 引用与致谢

详细的 B 站作者/视频链接/上游项目见 ACKNOWLEDGEMENTS.md。

- 梗库：B 站 in1t_，视频 BV1DMnS6kEkW，上游 in1t1a1/astrbot_plugin_memewiki 与 thawline/dsh-memewiki（MIT）。
- 看视频/分镜：B 站 苍穹晓雷，视频 BV1Hgx76SEqb，插件 13520rygvv/dsh-bili-video；上游 Yuuyuko-uu/dsh-bili-subagent、c1overzz/dsh-bili-scroll、SAOS-3624/dsh-video-storyboard（均为 MIT，按各自仓库为准）。

## 表情包

- B 站作者：可以叫我云大人 / 雾雪，视频 BV13xnt6QE6s，项目 https://github.com/Loping151/dsh-nekowhale-stickers
- 项目 README 标注 Apache-2.0 / CC BY-NC 4.0；本仓库不分发表情图片或数据，当前实例约 320 张由用户按作者项目自行接入。
