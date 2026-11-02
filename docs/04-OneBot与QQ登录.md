# 04 · OneBot 与 QQ 登录

## 1. QQ 小号

- 新注册/长期不用的小号；
- 不要在主力手机官方 QQ 上同时登录；
- 首次登录可能需要验证码/设备验证，尽量在常用网络下完成；
- 登录成功后不要频繁重建环境，否则可能再次要求扫码。

## 2. SnowLuma WebUI

在手机浏览器或 VM 内访问：

~~~text
http://127.0.0.1:5099
~~~

如果 DroidVM 端口转发已配置，手机浏览器也可以访问 127.0.0.1:5099。

用 QQ 小号扫码登录后，等待 SnowLuma 显示账号在线。

## 3. OneBot v11 WebSocket

在 SnowLuma 的 OneBot 设置中：

- 启用 WebSocket；
- 监听 3001（只给本机/VM 内使用）；
- 设置一个随机 token；
- 不要把这个端口暴露到公网。

bridge 默认连接：

~~~text
ws://127.0.0.1:3001
~~~

## 4. 验证

在 VM 里：

~~~bash
curl -s http://127.0.0.1:3000/get_login_info?access_token=你的token
~~~

能看到 bot 的 QQ/昵称即可。WS 是否正常以 bridge 日志的“OneBot WebSocket 已连接”为准。

## 5. 安全

- OneBot token 只放本地；
- bridge 的 onebotToken 必须与 SnowLuma 一致；
- 只监听 127.0.0.1，不要 0.0.0.0；
- 不要把 3001/5099 映射到公网；
- 不要把二维码/账号截图发到公开群。
