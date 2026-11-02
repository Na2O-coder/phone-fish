# 06 · DeepSeek 与主人权限

## 1. DeepSeek key

- 在 DeepSeek 开放平台申请；
- 充少量余额先测试；
- 不要把 key 写进 README/视频/Git；
- 用 bootstrap.py 写入 config.json 与 bridge.env，权限 600。

## 2. 主人 QQ

bootstrap 会把 permissions.ownerUsers 和 skills.groupAdmin.adminUsers 设成你的 QQ。

只有主人能使用敏感命令，例如：

~~~text
/状态 /日志 /配置 /重启桥 /规则 /日报 /缓存 /权限 /主动 /睡眠
~~~

管理只走 QQ，不提供网页后台改配置。

## 3. 回复策略

配置文件里与“像活人”相关的开关：

- autoReply：总开关、冷却、群聊合并窗口、插话概率；
- groupReply：botNames、是否引用、是否 @；
- sleep：睡眠时段、唤醒阈值、早上补回；
- memory/profiles/reflection：长期记忆、人物印象、反思；
- behavior.context：持久上下文、压缩阈值；
- deepseek.thinkingMode：auto 低强度任务不思考，高强度保留。

不要为了“每条都回”把冷却/频率关掉；QQ 风控和群体验都会变差。

## 4. 思考模式与成本

deepseek-flash 默认开启思考模式，思维链按输出 token 计费。

本包默认：

- low：普通聊天、反思、记忆抽取、人物印象、自复盘 → 关闭思考；
- high/max：复杂问题、核实模式、主人私聊 → 保留思考。

如果觉得非思考模式不够聪明，可以改成 thinkingMode=enabled；如果更在意成本，可以改成 disabled（但复杂问题质量可能下降）。

## 5. 缓存

- 缓存只影响输入价格；
- 切换 thinking/重启后第一次请求可能整段未命中，这是正常的；
- /缓存 看累计命中；
- 不要用关键词黑名单去“省 token”，那会破坏行为。

## 6. 安全

- 不要把 DeepSeek 控制台/账单截图原图发出去；
- 不要展示 API key；
- 不要在视频里展示 owner QQ/群号；
- 如果怀疑泄露，立刻在控制台吊销旧 key 并重新生成。
