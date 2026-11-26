# Vitals Mac agent

在登录后的 Mac 会话运行。每 3 秒向 Vitals 上报 CPU、内存、磁盘、网速、内存占用最高的 5 个进程、电池、系统音量及 Music / Spotify 状态。每 30 秒同步选定的 Apple 提醒事项列表。

## 安装和配置

需要 macOS、Python 3.10 或更新版本，以及已部署第九阶段 Part A 的 Windows Vitals。Windows 端的 `config.json` 必须设置 `agents.token`。从仓库根目录执行以下命令。

```bash
(umask 077; cp -n mac_agent/config.example.json mac_agent/config.json)
```

| 字段 | 用途 |
| --- | --- |
| `server_url` | Windows Vitals 的 Tailscale HTTPS 地址，例如 `https://<your-device>.<your-tailnet>.ts.net`。 |
| `token` | 和 Windows `agents.token` 相同的密钥。 |
| `device_id` | 同一台 Mac 使用固定 ID，默认 `macbook`。 |
| `device_name` | 网页上的设备名称，默认 `MacBook`。 |
| `reminders_list` | 精确的 Apple 列表名。`null` 使用系统的默认提醒事项列表。 |

填好后执行 `./mac_agent/install_launchd.sh`。脚本建立 `mac_agent/.venv`，安装依赖并加载当前用户的 LaunchAgent。此后在 macOS 登录时自动启动。请保留仓库在安装时的位置。日志写入 `~/Library/Logs/vitals-mac-agent.log`。

首次访问提醒事项、Music 或 Spotify 时，允许 macOS 的自动化请求。如果后台进程没有显示权限请求，可在 Terminal 前台运行 `mac_agent/.venv/bin/python mac_agent/agent.py --sync-reminders-once` 检查。请用下方的 `bootout` 暂停登录项后再运行该命令，避免两个进程同时同步。锁屏使用 Control–Command–Q，需要允许 System Events 的自动化和辅助功能权限。通知由 macOS 通知设置控制。

```bash
# 暂停本次登录会话的 agent
launchctl bootout "gui/$(id -u)/com.vitals.mac-agent"

# 只采集本机快照
mac_agent/.venv/bin/python mac_agent/agent.py --sample

# 同步一次并退出
mac_agent/.venv/bin/python mac_agent/agent.py --sync-reminders-once

# 重新加载登录项
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.vitals.mac-agent.plist"
```

卸载时先运行 `bootout`，再删除 `~/Library/LaunchAgents/com.vitals.mac-agent.plist`。

## 隔空剪贴板

```bash
mac_agent/.venv/bin/python mac_agent/clipboard.py push
mac_agent/.venv/bin/python mac_agent/clipboard.py pull
```

`push` 优先读取剪贴板中的图片并以 PNG 上传，或发送纯文字。`pull` 按创建时间选取最新一条文字，跳过文件及排序在前的旧置顶条目。两条命令成功后都会请求系统通知。绑定 Raycast、快捷指令或键盘快捷键时，用虚拟环境 Python 和 `clipboard.py` 的绝对路径。

## 提醒事项与网页控制

- 首次同步会上传选定列表中已有的提醒事项，也会把 Vitals 中待同步的待办写入该列表。可先使用专用测试列表。每个中心端应由一份 Mac agent 负责这个列表。两端的时钟需要同步。
- 同步字段为标题和完成状态，支持两端新增、勾选、取消勾选和删除。Vitals 标题上限是 200 字符。Windows 创建的提醒事项在备注首行保存用于重试和去重的 Vitals ID，请保留该行。同步状态保存在 `~/Library/Application Support/Vitals Mac Agent/`。
- 在 Vitals 的「控制」页选择 `MacBook`。播放器同时运行时，优先控制正在播放的应用，其次是暂停的应用。Mac 实际进入系统休眠后，网页会将它标为离线。休眠指令的回报可能在设备唤醒后送达。
- 心跳或提醒事项请求返回 404 时，检查 Windows 是否已运行 Part A。401 表示密钥不匹配，503 表示中心端尚未配置密钥。

## 测试

在仓库根目录运行以下命令。

```bash
mac_agent/.venv/bin/python -m unittest discover -s mac_agent/tests -v
```
