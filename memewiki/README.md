# 梗百科数据

- 数据来源：in1t1a1/astrbot_plugin_memewiki
- 整理版本：thawline/dsh-memewiki
- 许可：MIT（见 LICENSE）
- 文件：release asset memes.db（约 16 MB）
- 不包含表情图片/字体/第三方卡片。

## 使用

把 memes.db 放到：

~~~text
/opt/snowluma/bridge/data/memes.db
~~~

然后在 config.json 中确认：

~~~json
{
  "skills": {
    "memewiki": {
      "enabled": true,
      "dbPath": "/opt/snowluma/bridge/data/memes.db",
      "maxContextChars": 650
    }
  }
}
~~~

没有这个文件时，memewiki 会自动降级，不会影响 bridge 启动。
