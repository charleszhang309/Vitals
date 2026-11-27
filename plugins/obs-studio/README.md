# OBS Studio

将 `obs-studio` 目录放在 Vitals 主中心配置目录的 `plugins/` 下，把 `obs-studio` 加入主中心 `config.json` 的 `deck.enabled_plugins`，然后重启主中心。在 OBS 的「工具 → WebSocket 服务器设置」中启用 WebSocket 服务器。

有密码或需要连接局域网 OBS 时，在 **插件目录**里新建本地 `config.json`，填写 OBS 对应的连接信息。配置文件缺省时使用 `127.0.0.1:4455` 和空密码。连接设置只从主中心上的插件文件读取。更改设置后重启 Vitals 主中心。

```json
{
  "host": "192.168.1.25",
  "port": 4455,
  "password": "替换为 OBS WebSocket 密码"
}
```

`host` 可填 IP 地址或主机名。OBS 内置服务使用 `ws://`，局域网连接应使用可信网络。

- 切换场景时填写 OBS 中的完整场景名。静音动作填写音频源名。显示或隐藏画面源时填写源名，场景名留空则使用当前节目场景。
- 为 Deck 按键选择「录屏状态」可在录屏时显示 🔴。也可选择「推流状态」「当前节目场景」或「OBS 连接状态」。录屏、推流及当前节目场景也可作为变量使用。状态按 Deck 现有的轮询节奏更新。
- 断连时状态读数清空，「OBS 连接状态」显示离线。动作结果包含连接提示，可通过 Deck API 读取。之后的状态轮询或按键操作会尝试重新连接。

协议参考见 [OBS 官方 obs-websocket v5 文档](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md)。
