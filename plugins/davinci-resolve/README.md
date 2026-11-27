# DaVinci Resolve 剪辑

将 `davinci-resolve` 目录放在 Vitals 主中心配置目录的 `plugins/` 下，把 `davinci-resolve` 加入主中心 `config.json` 的 `deck.enabled_plugins`，然后重启主中心。

动作使用 Resolve 默认的键盘映射，通过 Vitals 向主中心所在电脑的前台窗口发送快捷键。使用时请让 Resolve 保持在前台，并按操作需要聚焦时间线或查看器。可在另一台设备上打开 Deck 来保持剪辑窗口的焦点。macOS 需要为 Vitals 授予辅助功能权限。

下列符号可直接填入 Deck 按键的图标栏。

| 动作 / Action | Windows | macOS | 建议图标 |
| --- | --- | --- | --- |
| 播放 / 暂停 · Play / Pause | Space | Space | ⏯ |
| 倒放 · Shuttle Reverse | J | J | ⏪ |
| 停止 · Shuttle Stop | K | K | ⏹ |
| 快进 · Shuttle Forward | L | L | ⏩ |
| 剪切刀模式 · Blade Edit Mode | B | B | ✂ |
| 在播放头分割 · Split at Playhead | Ctrl+B | Cmd+B | ✂️ |
| 波纹删除 · Ripple Delete | Shift+Backspace | Shift+Delete (⌫) | ⌫ |
| 添加标记 · Add Marker | M | M | ◆ |
| 撤销 · Undo | Ctrl+Z | Cmd+Z | ↶ |
| 重做 · Redo | Ctrl+Shift+Z | Cmd+Shift+Z | ↷ |
| 剪辑 · Edit | Shift+4 | Shift+4 | 🎬 |
| 调色 · Color | Shift+6 | Shift+6 | 🎨 |
| 音频 · Fairlight | Shift+7 | Shift+7 | 🎚 |
| 交付 · Deliver | Shift+8 | Shift+8 | 📦 |

重复按 J 或 L 可逐级提高对应方向的播放速度。快捷键参考见 Blackmagic Design 的 [编辑指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Editors-Guide.pdf)、[Fairlight 音频指南](https://documents.blackmagicdesign.com/UserManuals/DaVinci-Resolve-20-Fairlight-Audio-Post.pdf)和[页面快捷键参考](https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_14_Reference_Manual.pdf)。
