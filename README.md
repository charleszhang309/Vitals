# Vitals

单进程 FastAPI 应用，通过同一个 HTTP 端口提供今日概览、硬件监控、电源控制、中转站和个性化设置。首页为 `/`，API 文档为 `/docs`，实时硬件快照通过 `/ws/metrics` 推送。

## Windows 安装和启动

使用 Python 3.10 或更新版本，在项目根目录运行 `start_service.bat`。脚本会进入项目目录，按需创建和激活 `.venv`，并在缺少前端产物时构建。首次从源码构建需要 Node.js 20.19+（20.x）或 22.12+。也可以逐步执行以下命令。 [Vite 环境要求](https://vite.dev/guide/)

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if (-not (Test-Path config.json)) { Copy-Item config.example.json config.json }
Push-Location frontend
npm.cmd ci
npm.cmd run build
Pop-Location
& .\.venv\Scripts\python.exe -m backend.main
```

脚本按 `config.json` 中的 `host` 和 `port` 启动 Uvicorn。`host` 默认 `0.0.0.0`，`port` 默认 `3993`。初次可在本机打开 `http://127.0.0.1:3993/`。应用首次启动会创建 `data/app.db` 和 `data/uploads/`。配置使用标准 JSON，修改后重启服务。配置内的相对文件路径以项目根目录为基准。

### Tailscale 私有访问

使用 Tailscale Serve 提供 HTTPS 时，保留 `host: 0.0.0.0` 和 `port: 3993`，并在 tailnet 中启用 MagicDNS 与 HTTPS。管理员终端执行 `tailscale serve --bg 3993`，用 `tailscale serve status` 确认 `https://<your-device>.<your-tailnet>.ts.net/` 代理到 `http://127.0.0.1:3993`。Serve 会在 Tailscale 重启后恢复，Windows 开机任务只需启动 Vitals。 [Tailscale Serve 命令](https://tailscale.com/docs/reference/tailscale-cli/serve)

如需直连 HTTP，可按以下步骤配置。

1. 将 Windows 主机和浏览器所在设备加入同一个 tailnet。Windows 主机上运行 `tailscale ip -4` 获取服务地址。
2. 将 `config.json` 的 `host` 改为该 Tailscale IP。其他设备可访问 `http://<your-tailscale-ip>:3993/`，也可以使用该设备的 MagicDNS 名称。绑定单个 Tailscale IP 后，本机同样使用这个地址访问。 [Tailscale IP 与 DNS](https://tailscale.com/docs/concepts/ip-and-dns-addresses)
3. 如果 Windows 防火墙阻止连接，为配置的 TCP 端口添加入站规则，并将规则限定在 Tailscale 接口。用 tailnet 访问策略限制可连接该端口的设备或用户。保持路由器端口转发和 Tailscale Funnel 关闭。 [Tailscale 访问策略示例](https://tailscale.com/docs/reference/examples/acls)

服务检查直连客户端地址和浏览器请求来源。保留 `0.0.0.0` 时，应用接受本机回环与 Tailscale 地址段的请求。应用没有独立登录，能访问该端口的成员也能读取健康概览、操作电源计划与中转站。请通过 tailnet 访问策略只授权信任的设备。以直连 HTTP 部署，维持 Uvicorn 的代理头处理关闭。

### LibreHardwareMonitor

启动 LibreHardwareMonitor，并启用 `Options` → `Remote Web Server` → `Run`。将 LHM Web 服务器绑定到本机回环地址，保持其端口仅供本机访问。用 `Invoke-RestMethod http://localhost:8085/data.json` 检查传感器树。端口或路径不同时，修改配置中的 `lhm.url`。部分传感器需要 LHM 以管理员身份运行。 [LHM 项目与下载](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)

监控每 1.5 秒采样一次。CPU、GPU、内存和风扇优先使用 LHM。系统采集补充磁盘容量、网络速率与累计流量、网卡地址和内存占用最高的 5 个进程。LHM 断开时，系统继续提供 CPU 占用、内存、磁盘和网络，温度、功耗等缺失读数显示为「—」。断开后每 5 秒重试。

「风扇转速」按 CPU、GPU 和机箱分组，每组显示当前最高转速，忽略 0 RPM。对于仅显示编号的主板接口，可以在 `lhm.fan_mapping` 中用 LHM 树中的 `SensorId` 指定分组，例如 `{"/lpc/nct6798d/0/fan/1": "cpu"}`。也可使用 `"Fan #2"` 或 `"2"`，在多块控制板上建议使用完整 ID。支持 `cpu`、`gpu`、`chassis` 和 `pump`。水泵的转速显示在 CPU 详情。

### 电源计划与 FanControl

运行以下命令查看本机的电源计划。将所选 GUID 填进 `power_modes` 中对应的 `guid`。要用 Bitsum Highest Performance，找到本机列表中该方案的 GUID，再填入 `power_modes.performance.guid`。示例配置提供 Windows 内置计划，实际可选计划以本机输出为准。 [Microsoft powercfg 命令](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options)

```powershell
powercfg /list
powercfg /getactivescheme
```

要为某个模式调用 FanControl，设置根级 `fancontrol_exe`，并给该模式设置 `fan_profile`。JSON 路径中的反斜杠写成 `\\`，也可使用正斜杠，例如 `<path-to-fancontrol>/FanControl.exe`。切换时先执行 `powercfg /setactive`，再派发 `FanControl.exe -c <profile.json>`。`dispatched` 表示 Windows 已启动命令。若 FanControl 派发失败，响应仍会报告新电源计划和 `fan_control.status: failed`，客户端可以显示具体结果。FanControl 需要在能访问相应用户会话的账号下运行。 [FanControl 命令行配置文档](https://github.com/Rem0o/FanControl.Releases/wiki/Hotkeys---Automate-config-changes-%28V136-and-up%29)

Fan Control 作为 Windows 服务运行时，可启用 `fancontrol_mode_sensor`，让 Vitals 按实际电源计划更新 `data/fancontrol-mode.sensor`。Fan Control 的 File 传感器读取该文件，将数值映射为低温档位，并与独立的真实温控曲线取较高的值。静音和均衡档位分别写入 30 和 45，性能档写入 0 以交由真实温控曲线调速。`sensor_updated` 表示档位文件已更新，实际转速可以在硬件监控中确认。 [Fan Control 文件传感器与混合曲线](https://getfancontrol.com/docs/)

### 任务检测与通知

| 配置 | 默认行为 |
| --- | --- |
| `task.source` | `auto`，分别计时 CPU 与各 GPU，跟踪第一个满足条件的设备。也可设为 `cpu` 或 `gpu`。 |
| `task.high_percent` / `task.high_seconds` | 同一设备负载持续高于 70% 达 300 秒，状态进入 `running`。 |
| `task.low_percent` / `task.low_seconds` | 运行中的同一设备负载持续低于 10% 达 120 秒，状态进入 `completed` 并发送通知。 |
| `alerts.cpu_temperature_c` / `alerts.gpu_temperature_c` / `alerts.temperature_seconds` | 同一设备持续超过 90°C 达 30 秒时告警。 |
| `alerts.disk_free_gib` | 任一分区剩余空间低于 15 GiB 时告警。 |
| `alerts.cooldown_seconds` | 每个 CPU、GPU 和磁盘分区各自执行 1800 秒的告警冷却。 |

完成状态保留到下一项任务进入 `running`。任务和告警状态存放在进程内，重启后重新计时。短暂越线、传感器不可用或采样间隔超过 10 秒会打断连续计时。任务运行期间，若目标设备连续缺失读数达 60 秒或配置的 `low_seconds`（取较大值），任务回到 `idle`，不会发送完工通知。磁盘和温度告警在各自冷却后仍满足条件时可以再次发送。

- Bark 通知将 `webhook.kind` 改成 `bark`，替换 URL 模板中的设备密钥。服务会将事件标题和消息编码后填进 `{title}` 与 `{body}`，再发送 GET 请求。 [Bark 项目文档](https://github.com/Finb/Bark)
- 通用通知将 `webhook.kind` 改成 `json`，并填写 `webhook.json_url`。服务向该地址 POST `{"type":"task_completed","title":"任务已完工","message":"…","timestamp":"2026-01-01T00:00:00.000+00:00"}`。其他事件类型为 `cpu_temperature`、`gpu_temperature` 和 `disk_space`。
- 默认 `disabled`。Webhook 由后台队列派发，发送失败会记录事件类型和异常类型。每个入队事件发送一次，持续告警可在下一次冷却期结束后再次触发。

安装 Bark 后，从 iPhone 的 Bark 应用复制设备密钥，在服务器 `config.json` 中使用如下配置并重启。使用自建 Bark 时，将 `api.day.app` 换成对应的服务地址。

```json
"webhook": {
  "kind": "bark",
  "bark_url_template": "https://api.day.app/设备密钥/{title}/{body}",
  "timeout_seconds": 5
}
```

在已连接 tailnet 的设备上用 `curl.exe -X POST https://<your-device>.<your-tailnet>.ts.net/api/control/webhook/test` 发送一条测试通知。返回 `sent: true` 表示 Bark 服务器接受了推送，再查看 iPhone 是否收到。可选配置被禁用时该接口返回 `409`。

在 `webhook.daily_briefing_time` 设置 `"08:00"`，即可按 `health.timezone` 每早通过 Bark 接收明日香简报。推送沿用 AI 和「附带 Vitals 实时状态」开关，并与首页共用当天缓存。服务若在设定时间后一小时内恢复，会补发仍未尝试推送的当天简报。每个自然日最多尝试一次 Bark 派发。设为 `null` 可关闭简报推送。修改配置后重启 Vitals。

## API

| 方法和路径 | 请求及响应 |
| --- | --- |
| `GET /api/control/power` | 当前计划的 `guid`、`name`、匹配的 `mode` 和可选 `modes`。当前计划未列入配置时，`mode` 为 `null`。 |
| `POST /api/control/power` | JSON `{"mode":"balanced"}`。仅接受配置中的模式名。返回 `guid`、`mode` 和 `fan_control`。 |
| `GET /api/scratchpad` | 返回按创建时间倒序排列的记录。 |
| `POST /api/scratchpad/text` | JSON `{"text":"跨设备复制这段文字"}`。返回创建的记录，状态码 `201`。 |
| `POST /api/scratchpad/file` | `multipart/form-data`，文件字段 `file`。返回创建的记录，状态码 `201`。超过 `upload_limit_mib` 返回 `413`，默认上限 50 MiB。 |
| `GET /api/scratchpad/files/{id}` | 常见位图格式默认内嵌预览，其余文件以附件下载。对任意文件追加 `?download=true` 可强制下载。 |
| `DELETE /api/scratchpad/{id}` | 删除记录及关联文件，成功返回 `204`。Windows 上文件正在使用时可能返回 `409`，可稍后重试。 |
| `PATCH /api/scratchpad/{id}/pin` | JSON `{"pinned":true}` 可固定，`false` 可取消。省略请求体会切换状态。 |
| `GET` / `PATCH /api/scratchpad/settings` | 读取或更新 `retention_days`（`3`、`7`、`30` 或 `null`）。 |
| `GET /api/weather` | 天气与美国 AQI，包含缓存状态和最近更新时间。 |
| `POST /api/health/ingest` | 接收包含 `data.metrics` 的 Health Auto Export JSON v2，单次最大 32 MiB。 |
| `GET /api/health/summary` | 返回今日活动、心率趋势、最新测量与最近睡眠。 |
| `GET /api/ai/status` | 前端能力开关，只返回有效启用状态。 |
| `POST /api/ai/analyze` | 按 JSON `{"id":文本卡片编号}` 整理该条文本。 |
| `POST /api/ai/command` | 接收 `{"command":"切到静音模式"}`，执行已匹配的本机快捷控制。 |
| `GET /api/wallpaper` | 共享壁纸版本 `version`，无壁纸时为 `null`。 |
| `GET /api/wallpaper/image` | 获取当前壁纸的 WebP 内容。 |
| `PUT /api/wallpaper` | 请求体传入 PNG、JPEG 或 WebP 静态图片原始字节，最大 10 MiB。返回新版本。 |
| `DELETE /api/wallpaper` | 删除共享壁纸，成功返回 `204`。 |

中转记录字段为 `id`、`kind`（`text` 或 `file`）、`text`、`filename`、`size_bytes`、`mime_type`、`created_at` 和 `pinned`。不适用的字段返回 `null`。记录先按固定状态排序，再按创建时间倒序。固定记录免于自动清理，仍能手动删除。保留期设置存入 SQLite，默认关闭，启用后每小时清理一次过期记录与对应文件。

`/ws/metrics` 无需订阅消息，连接后自动收到最新快照。前端可以显示以下字段。

| 字段 | 内容 |
| --- | --- |
| `timestamp`, `lhm_connected` | UTC 采样时间和 LHM 连接状态。 |
| `cpu` | `usage_percent`、`temperature_c`、`power_w`。 |
| `gpus[]` | `id`、`name`、`usage_percent`、`temperature_c`、`power_w`、`vram_used_bytes`、`vram_total_bytes`。 |
| `fans[]` | `id`、`name`、`device`、`rpm`。 |
| `memory` | `used_bytes`、`total_bytes`、`percent`。 |
| `disks[]` | `device`、`mountpoint`、`free_bytes`、`total_bytes`、`percent`。 |
| `network` | `upload_bytes_per_sec` 和 `download_bytes_per_sec`，基于本机网卡计数器。 |
| `task` | `state`、`source`、`started_at`、`completed_at`，其中 `source` 为 `cpu` 或触发任务的 GPU ID。 |
| `last_alerts[]` | 各类告警最近一次触发事件，结构同 JSON Webhook。 |
| `power` | 当前电源计划和匹配的预设模式。初次读取前为 `null`。 |

## 天气与健康导入

`weather.city_name` 是显示名称，`weather.latitude` 和 `weather.longitude` 用于请求 Open-Meteo。模板以伦敦为示例，部署时请填入所需城市的经纬度。天气与空气质量分别缓存 20 分钟。请求失败时保留上一份数据并标记为缓存，无缓存时显示暂不可用。 [Open-Meteo 天气预报](https://open-meteo.com/en/docs) · [Open-Meteo 空气质量](https://open-meteo.com/en/docs/air-quality-api)

在 iPhone 的 Health Auto Export 中新增 **REST API** 自动化，数据类型选择 **Health Metrics**，格式选择 **JSON / Version 2**，URL 填 `http://<your-tailscale-ip>:3993/api/health/ingest`。建议按小时分组，并勾选需要的指标。若配置 `health.ingest_token`，在自动化的 HTTP Headers 添加 `Authorization: Bearer <令牌>`。可以先手动发送一次历史记录检查导入。 [REST API 设置](https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/) · [Health Metrics JSON 格式](https://help.healthyapps.dev/en/health-auto-export/export-format/health-metrics/)

如果使用 Google Drive 本地同步，将 `health.export_dir` 设为 Windows 上的实际同步目录，例如 `<your-sync-folder>/Health Auto Export`。服务启动时及之后每两分钟检查最新的两份 `HealthAutoExport-*.json`。本地文件和直推共用 `data/app.db`，按指标与时间去重，重复项采用后写入的记录。已导入的文件版本会持久化记录，重启后也会跳过未变化的文件。`health.timezone` 决定每日的边界，默认为 `UTC`，可按需要填写 IANA 时区，例如 `Europe/London`。

健康详情接口为 `GET /api/health/detail/{category}`，类别为 `heart`、`activity`、`steps` 和 `sleep`。详情使用实际导入的采样。旧导出缺少的新指标会在下次导入相应数据后显示。要查看每小时明细，建议在 Health Auto Export 中选用每小时汇总或原始采样。睡眠趋势按醒来日期归档。

## 可选 AI

在服务器 `config.json` 的 `ai.api_key` 中填入 OpenAI 项目密钥。`ai.model` 默认为 `gpt-6-luna`，可改为项目有权限使用且支持 Chat Completions 的 OpenAI 模型。默认地址为 `https://api.openai.com/v1`，只支持 OpenAI 官方端点。更换密钥或模型后重启 Vitals。个性化设置里的启用开关和实时状态开关保存后立即生效。 [GPT-6 Luna 官方文档](https://developers.openai.com/api/docs/models/gpt-6-luna)

聊天使用 SSE，最近 50 条消息保存在本机 SQLite，供同一 Vitals 实例上的设备同步。开启「附带 Vitals 实时状态」时，发送问题会把精简的天气、健康和主机摘要交给 OpenAI。点「整理中转站最新内容」，或发送「帮我看看中转站刚存的东西」等明确的请求，会附带最近五条记录的文本片段或文件名。原有「AI 整理」只发送所选卡片。

聊天和「AI 整理」共用 `backend/agent.md` 中的角色设定。保存文件后，下一次发送或整理时会读取新设定。

启用 AI 且打开「附带 Vitals 实时状态」时，首次打开今日概览会向 OpenAI 发送精简的天气、昨晚睡眠和当日健康摘要，生成一则明日香简报。`GET /api/ai/briefing` 按 `health.timezone` 的自然日缓存，点刷新图标或访问 `?refresh=1` 可重新生成。健康区「7 天对比」比较最近两个完整的本地 7 天区间，缺记录的日子从均值中排除，睡眠按醒来日期归档。

中转站可在浏览器预览 PDF、DOCX、XLSX 和 CSV。超过 15 MB 的文件直接下载。移动端 PDF 使用新标签页打开。表格预览最多显示 500 行和 60 列，完整内容可通过下载查看。

在中转站输入 `/cmd 切到静音模式` 或 `/ai 30分钟后关机`。快捷控制在本机匹配预设规则，支持静音、平衡、性能、1–1440 分钟后关机以及取消定时关机。待执行的关机计划保存在进程内，服务重启会取消。到达时间时调用 Windows 的普通关机。 [Windows shutdown 命令](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/shutdown/)

硬件监控页也能安排 30 或 60 分钟后关机、取消计划、锁屏及关闭显示器。锁屏和定时关机需二次确认。部署时，在项目目录运行 `powershell.exe -NoProfile -File .\windows\install_vitals_desktop_task.ps1`，为 Vitals 开机任务的账号安装按需运行的 `Vitals Desktop Control`。锁屏和关闭显示器需要该账号已有交互式登录会话。 [Windows 计划任务交互令牌](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype)

## iPhone 主屏幕与分享菜单

在连上 Tailscale 的 iPhone 上，用 Safari 打开 `https://<your-device>.<your-tailnet>.ts.net/`，点「分享 → 添加到主屏幕」。若显示「作为网页 App 打开」，保持开启。之后可从主屏幕图标全屏打开 Vitals，功能需要连接 Windows 主机。

在「快捷指令」中可分别建立「发送文字到 Vitals」和「发送文件到 Vitals」。打开快捷指令详情里的「在共享表单中显示」，然后按输入类型设置：

| 快捷指令 | 分享表单接受 | 「获取 URL 内容」的配置 |
| --- | --- | --- |
| 发送文字到 Vitals | 文本、URL | `POST https://<your-device>.<your-tailnet>.ts.net/api/scratchpad/text`，请求体选「JSON」，添加文本键 `text`，值设为快捷指令输入。分享 URL 时可先加「从输入中获取文本」。 |
| 发送文件到 Vitals | 图像、文件 | `POST https://<your-device>.<your-tailnet>.ts.net/api/scratchpad/file`，请求体选「表单」，添加类型为「文件」的键 `file`，值设为快捷指令输入。每次只发一件，也可放进「对每个项目重复」逐个发送。 |

表单请求由快捷指令生成 multipart 边界。成功返回 HTTP `201` 和带 `id`、`kind` 的 JSON 记录，可在最后加「显示通知」提示已发送。 [Apple 快捷指令请求体说明](https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/ios)

## Windows 媒体与音量控制

在 Windows 登录后运行以下命令，为 `Vitals Autostart` 使用的账号安装交互式媒体任务。它使用随机回环端口与 Vitals 主服务通信，Tailscale 继续只代理 3993。重新登录会自动启动媒体任务。

```powershell
& .\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
& .\windows\install_vitals_media_task.ps1
Start-ScheduledTask -TaskName 'Vitals Media Bridge'
```

在「控制」里切换播放器后，播放和切歌命令都会发给选中的会话。可用功能取决于播放器是否将会话和对应操作发布到 Windows SMTC。关闭播放器或退出 Windows 时，页面会显示无会话或等待桌面会话，硬件监控继续运行。系统音量作用于 Windows 当前的默认播放设备。音量卡片可切换已启用的扬声器或耳机，切换作用于系统默认和多媒体输出，通信设备设置独立。 [Microsoft SMTC 接口](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession) · [Pycaw](https://github.com/AndreMiras/pycaw)

打开「正在播放」后，Vitals 先查本机 SQLite。Apple Music 优先使用公开 AMLL TTML 词库，QQ 音乐、网易云和酷狗优先使用各自词库。优先源未命中时，回退到 QQ 音乐、酷狗、网易云和 LRCLIB 的并行匹配。按歌名、歌手、专辑和时长评分，并区分现场和录音室版本。AMLL 会按词库提供的逐字或逐行时间轴渲染同步歌词，兼容可用的翻译和纯文本。无歌词时播放器自动居中并隐藏歌词按钮，切歌后恢复上次的开关状态。 [AMLL TTML API](https://amll.dev/reference/http-api/overview)

## 前端构建

更新前端后，在 `frontend/` 中执行 `npm ci` 和 `npm run build`。Vite 将产物写入 `backend/static/`，并清理旧构建产物。首次生成后重启应用，通过 `/` 访问。REST API 与 WebSocket 始终使用页面的同源地址。 [Vite 构建配置](https://vite.dev/config/build-options)

壁纸存放在 `data/wallpaper.webp`，所有设备打开页面时读取同一张图，保持页面打开也会自动同步。主题、遮罩和玻璃强度保存在当前浏览器。中转站可粘贴截图，文本卡片在剪贴板写入不可用时会选中文字，便于在私网 HTTP 下手动复制。

## 后端回归测试

```powershell
& .\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
& .\.venv\Scripts\python.exe -B -m pytest -q
```

## 任务计划程序开机自启

1. 在任务计划程序创建 `Vitals Autostart`，选用能访问项目目录的 Windows 账号。需要登录前运行时，选择**不管用户是否登录都要运行**并在**触发器**中添加**启动时**。Windows 端还需启用 Tailscale 的 **Run unattended**。 [Windows 启动触发器](https://learn.microsoft.com/en-us/windows/win32/taskschd/starting-an-executable-on-system-boot) · [Tailscale 无人值守模式](https://tailscale.com/docs/how-to/run-unattended)
2. 在**操作**选择**启动程序**。程序填写 `powershell.exe`，参数填写 `-NoProfile -NonInteractive -WindowStyle Hidden -File "<vitals-project-folder>\start_vitals_autorun.ps1"`，**起始于**填写 `<vitals-project-folder>`。脚本仅在绑定指定网卡 IP 时等待该地址可用，再调用 `start_service.bat`。
3. 在**设置**中启用失败后重试，选择**不启动新实例**，并允许任务持续运行。用**登录时**也可作备用触发器。日常由同一个计划任务管理服务。Windows 下手动重复启动时，后端会在初始化数据库和后台任务前检查独占端口，若端口已被占用便退出。若需要与桌面 FanControl 进程交互，可单独选择仅在同一账号登录时运行。
4. 保存后先手动运行任务，检查同一 tailnet 中是否能打开 Vitals。随后可在 Windows 的登录界面通过手机确认，重启后无人登录时也能访问。
