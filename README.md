<div align="center">

# Vitals

**轻量级 Windows 个人状态台与跨设备控制台**  
集「今日概览」「媒体与音量控制」「Windows 实时硬件监控」「跨设备图文/文件中转站」「可选 AI 助手」于一体，采用极简卡片与 Liquid Glass（液态玻璃）设计。

![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6?style=flat-square&logo=windows)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?style=flat-square&logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19%20%2B%20Vite-61DAFB?style=flat-square&logo=react&logoColor=black)
![License](https://img.shields.io/badge/License-MIT-34C759?style=flat-square)

</div>

---

## 核心功能

### 1. 今日概览
- **快捷状态条**：顶部直达主机实时状态与当前正在播放的媒体。
- **天气与空气质量**：接入 Open-Meteo，详情包括紫外线指数、US AQI、24 小时曲线与未来 7 天预报。
- **个人健康状态**：从 iPhone Health Auto Export 和 Android Health Connect 导入步数、睡眠、心率和活动能量。详情保留小时明细、趋势、血氧、呼吸频率与 7 天对比。
- **生活小组件**：双向日历、今日待办与微习惯、倒数日，以及支持换算和历史走势的参考汇率。在「个性化设置 → 今日概览」可分别开关状态条、简报和六张卡片。偏好保存在当前浏览器，关闭模块会暂停该模块的首页轮询。
- **每日一句 AI 状态简报（可选）**：按本地自然日缓存，结合天气、健康与今日日程，支持 Bark 定时推送。

### 2. 控制中心
- **跨播放器媒体控制**：基于 Windows 原生 **SMTC**，支持 Apple Music、QQ 音乐、网易云音乐、Spotify 与主流浏览器，支持多会话切换与切歌/播放控制。
- **Apple Music 风格全屏播放 + AMLL 动态歌词**：点击「正在播放」进入全屏氛围界面，支持多源智能歌词匹配（按当前播放器优先选源，覆盖 AMLL TTML / QQ 音乐 / 酷狗 / 网易云 / LRCLIB）与 `@applemusic-like-lyrics/react` 动态渲染；无歌词时自动居中并隐藏歌词按钮。
- **系统音量与输出设备**：实时调节 Windows 主音量、一键静音及切换音频输出设备。

### 3. 硬件监控与远程控制
- **精准传感器数据**：优先读取本地 **LibreHardwareMonitor**（CPU / GPU / 内存 / 风扇温度、功耗、频率与转速），`psutil` 补充磁盘空间、网络速率、累计流量与**内存占用 Top 5 进程**，LHM 断开时自动兜底。
- **实时双模曲线**：支持「占用 / 温度」分段切换，CPU / GPU / 内存 / 网络 4 张主卡片均可点击查看深度详情（含各核心负载）。
- **白名单快捷控制**：「控制」页可展开全屏硬件监控，切换电源计划（可选联动 FanControl）、锁屏或关闭显示器。支持确认后 1 分钟关机、30 / 60 分钟预设及 1–1440 分钟自定义定时，倒计时结束前可取消。
- **任务检测与告警推送**：支持高负载任务完工检测、高温与低磁盘空间告警，通过 Bark 或通用 JSON Webhook 推送到手机。

### 4. 跨设备中转站
- 支持文本、图片、文件跨设备存取，`Ctrl+V` 直接粘贴截图，支持单卡片固定与定期自动清理。
- **纯前端文档预览**：液态玻璃弹窗直接预览 **PDF、DOCX、XLSX、CSV**（15 MB 以内，不依赖 LibreOffice 或云端服务）。

### 5. 个性化与移动端体验
- **Liquid Glass 液态玻璃**：支持浅色、深色及跟随系统主题、自定义跨设备共享壁纸、玻璃强度与遮罩透明度调节。
- **全屏时钟与专注计时**：从顶栏时间进入全屏时钟，支持翻页、极简数字和模拟表盘。表盘可在个性化设置中切换，专注计时完成后计入今日统计。
- **PWA 原生体验**：配合 Tailscale Serve HTTPS，在 iPhone / iPad Safari「添加到主屏幕」后可全屏独立运行；支持 iOS 快捷指令分享菜单一键传文字/文件。
- **可选 AI 聊天浮窗**：支持 OpenAI 流式对话、跨设备 SQLite 对话同步、`backend/agent.md` 热加载角色人设，以及一键附带 Vitals 实时状态上下文。

---

## 快速开始

### 环境要求
- **Windows 10 / 11**
- **Python 3.10+**
- **Node.js 20.19+** 或 **22.12+**（首次从源码构建前端所需）

### 一键启动
在项目根目录直接双击或运行：

```powershell
.\start_service.bat
```

脚本会自动创建并激活 `.venv`、安装后端依赖、复制 `config.example.json` 为 `config.json`、构建前端静态产物至 `backend/static/`，并启动服务。  
启动后在本机访问 `http://127.0.0.1:3993/`（API 文档位于 `/docs`）。

<details>
<summary>手动分步安装与前端构建命令</summary>

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

运行后端回归测试：

```powershell
& .\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
& .\.venv\Scripts\python.exe -B -m pytest -q
```

</details>

---

## 进阶配置

### Tailscale 私有 HTTPS 访问（推荐）
Vitals 默认仅允许本机回环与 Tailscale 私有网络访问（内置 `PrivateNetwork` 中间件校验）。

1. 保持 `config.json` 中的 `host: "0.0.0.0"` 与 `port: 3993`。
2. 在 Tailscale Admin Console 的 **DNS** 页面启用 **MagicDNS** 与 **HTTPS**。
3. 在 Windows 管理员终端运行：
   ```powershell
   tailscale serve --bg 3993
   ```
4. 使用 `tailscale serve status` 获取专属地址 `https://<your-device>.<your-tailnet>.ts.net/`，手机和 iPad 直接访问即可获得正版 HTTPS 证书与 PWA 全屏体验。

<details>
<summary>LibreHardwareMonitor、电源计划与 FanControl 配置</summary>

- **LibreHardwareMonitor**：以管理员身份运行 LHM，启用 `Options → Remote Web Server → Run`（默认 `http://localhost:8085/data.json`）。主板自定义风扇编号可在 `config.json` 的 `lhm.fan_mapping` 中映射为 `cpu`、`gpu`、`chassis` 或 `pump`。
- **电源计划**：运行 `powercfg /list` 查看本机 GUID，填入 `config.json` 的 `power_modes`。
- **FanControl 联动**：设置 `fancontrol_exe` 与各模式的 `fan_profile`（或启用 `fancontrol_mode_sensor` 输出传感器文件）。
- **锁屏与关屏桌面任务**：运行 `powershell.exe -NoProfile -File .\windows\install_vitals_desktop_task.ps1` 安装交互式桌面控制任务。

</details>

<details>
<summary>iPhone、Android 健康数据导入与天气配置</summary>

- **天气**：在 `config.json` 中修改 `weather.city_name`、`latitude`、`longitude`（模板以伦敦为例），缓存 20 分钟。
- **Health Auto Export 直推**：在 iPhone App 中新建 **REST API** 自动化，格式选 **JSON / Version 2**，URL 填 `https://<your-device>.<your-tailnet>.ts.net/api/health/ingest`（如配置了 `health.ingest_token`，添加 Header `Authorization: Bearer <token>`）。
- **HCGateway / Health Connect JSON**：让 Android 应用先把数据同步到 HCGateway，脚本从 `/api/v2/fetch/{method}` 取回记录，再将如 `{"type":"steps","records":[...]}` 的 JSON 发到上面的 Vitals 导入地址。分指标读取时用 `type` 保留原方法名，例如 `heartRate`、`sleepSession`、`activeCaloriesBurned`。已有导出工具时，可直接发送带指标名的 JSON。 [HCGateway API](https://github.com/ShuchirJ/HCGateway/blob/main/doc/api-documentation.yml)
- **Tasker / 自定义脚本**：在 Android 读取 Health Connect 后，用 Tasker 的「HTTP Request」执行 `POST`。请求头设为 `Content-Type: application/json`，配置过 `health.ingest_token` 时再加 `Authorization: Bearer <token>`。例如下方请求体。
- **Health Sync 同步目录**：在 Android Health Sync 选择 Health Connect 到 Google Drive 的导出，并在 Windows 用 Google Drive 桌面版同步该目录。将本机 `config.json` 的 `health.export_dir` 指向实际目录后重启。Vitals 每两分钟扫描目录与子目录，识别常见的日期、时间、步数、心率、睡眠和能量列，支持 CSV 及结构化 JSON。iPhone 的 `HealthAutoExport-*.json` 可继续放在同一目录。 [Health Sync 导出说明](https://healthsync.app/about/)

```json
{
  "metrics": [
    { "type": "steps", "value": 8500, "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T18:00:00Z" }
  ]
}
```

无时区的日期或时间按 `health.timezone` 解释。相同指标和时间的记录共用同一条 SQLite 数据。同步每日累计值时请保持同一天的 `start` 一致，并为同一指标选择同一种采样粒度，避免日累计和覆盖同一时间的逐笔数据同时计入。总消耗在活动明细 API 中单独记录，活动能量仍使用活动消耗数据。

</details>

<details>
<summary>日历同步与生活小组件</summary>

在本机 `config.json` 的 `calendar.sources` 添加所需对象后重启。`id` 须唯一，`color` 使用 `#RRGGBB`，`writable: false` 可将任何来源设为只读。服务每 5 分钟同步一次，覆盖今天至未来第 14 天。`health.timezone` 同时决定日历分组、待办与习惯跨日、倒数日和专注累计的日期。

| `type` | 需要配置的字段 |
| --- | --- |
| `icloud` | `apple_id`、`app_password`、`calendar_id`（可选，填写日历名称或 ID） |
| `google` | `client_id`、`client_secret`、`refresh_token`、`calendar_id`（可选，默认 `primary`） |
| `outlook` | `client_id`、`refresh_token`、`client_secret`（机密客户端需要）、`tenant`（可选，默认 `common`）、`calendar_id`（可选，默认主日历） |
| `ics` | `url`（HTTPS 或 `webcal://`，后者转换为 HTTPS）；始终只读 |

例如，将下列对象放入 `calendar.sources`，替换其中的占位符后保存：

```json
{
  "id": "apple",
  "name": "iCloud 日历",
  "color": "#007AFF",
  "type": "icloud",
  "apple_id": "<apple-account>",
  "app_password": "<app-specific-password>"
}
```

1. **iCloud**：在 Apple 账户启用双重认证，从「登录与安全性 → App 专用密码」为 Vitals 生成密码。将 Apple 账户填入 `apple_id`，专用密码填入 `app_password`。 [Apple App 专用密码说明](https://support.apple.com/en-us/102654)
2. **Google**：在 Google Cloud 启用 Calendar API，配置 OAuth 同意页与客户端。使用同一客户端 ID 和登记的重定向 URI 执行授权码流程，请求 `https://www.googleapis.com/auth/calendar.events`、`access_type=offline` 和 `prompt=consent`。在服务端把授权码交换为令牌，将得到的 `refresh_token` 与客户端凭据填入配置。 [Google OAuth 服务端流程](https://developers.google.com/identity/protocols/oauth2/web-server) · [Calendar API 事件权限](https://developers.google.com/workspace/calendar/api/auth)
3. **Outlook**：在 Microsoft Entra 注册应用并登记重定向 URI。添加 Microsoft Graph 委派权限 `Calendars.ReadWrite`，授权码流程请求 `https://graph.microsoft.com/Calendars.ReadWrite offline_access`。用注册时的客户端和重定向 URI 交换令牌，将 `refresh_token` 写入本机配置。组织账户可使用对应租户 ID。 [Microsoft 授权码流程](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)

订阅链接、App 专用密码和 OAuth 凭据只填本机 `config.json`。启用 AI 的「附带 Vitals 实时状态」时，聊天和每日简报将发送当天最多 8 条日程的标题与起止时间给配置的 OpenAI 服务。地点和备注留在 Vitals。

省略 iCloud 的 `calendar_id` 时，Vitals 合并账号下所有支持 `VEVENT` 的子日历。新日程写入第一个可写的子日历，编辑和删除操作写回事件所属的子日历。

待办、习惯日志、倒数日与专注记录保存在 `data/app.db`。专注计时可选 15、30、45 分钟，或自定义 1～180 分钟的整数。进行中的计时保存在当前浏览器，完成记录跨设备同步。

汇率支持双向输入、互换币种与固定最多 5 组汇率对。排序保存在 `data/app.db`，首页只显示第一组。首次使用时按 `finance.base_currency` 和 `finance.target_currencies` 初始化固定列表，默认是 CAD → CNY、CAD → USD。7 天、30 天和 1 年使用 Frankfurter 日参考汇率，24 小时优先显示本机小时快照，样本不足时显示最近可用汇率。参考汇率缓存一小时。 [Frankfurter API](https://frankfurter.dev/)

</details>

<details>
<summary>AI 助手、Bark 推送与 Windows 开机自启</summary>

- **OpenAI 助手**：在 `config.json` 中配置 `ai.api_key`（默认 `gpt-6-luna`，官方端点 `https://api.openai.com/v1`），在网页右上角「个性化设置」中控制总开关与实时状态联动开关。编辑 `backend/agent.md` 可热加载自定义角色人设。
- **Bark 推送**：在 `config.json` 中将 `webhook.kind` 设为 `"bark"`，将 `bark_url_template` 设为 `https://api.day.app/<YOUR_DEVICE_KEY>/{title}/{body}`，重启后可通过 `POST /api/control/webhook/test` 测试连通性。
- **iPhone 快捷指令**：在「快捷指令」中新建共享表单指令，文本/URL `POST` JSON `{"text": ...}` 至 `/api/scratchpad/text`，图片/文件 `POST` 表单字段 `file` 至 `/api/scratchpad/file`。
- **任务计划程序开机自启**：创建计划任务，程序填 `powershell.exe`，参数填 `-NoProfile -NonInteractive -WindowStyle Hidden -File "<项目路径>\start_vitals_autorun.ps1"`，起始于项目根目录，并启用 Tailscale 的 **Run unattended**。

</details>

<details>
<summary>主要 API 端点一览</summary>

| 方法与路径 | 说明 |
| --- | --- |
| `WS /ws/metrics` | 实时推送硬件快照（CPU / GPU / 内存 / 磁盘 / 网络 / 风扇 / 告警） |
| `GET / POST /api/control/power` | 查询或切换 Windows 电源计划 |
| `POST /api/control/webhook/test` | 发送 Bark / Webhook 测试通知 |
| `GET / POST /api/media/...` | 读取/控制 Windows SMTC 媒体会话、多源歌词与系统音量 |
| `GET / POST / DELETE /api/scratchpad...` | 中转站文本/文件存取、固定、预览与清理设置 |
| `GET /api/weather` | 获取天气、未来预报与 US AQI |
| `POST /api/health/ingest` & `GET /api/health/...` | 导入 Apple / Android 健康 JSON 与查询健康摘要/详情 |
| `GET / POST /api/calendar/events` & `PATCH / DELETE /api/calendar/events/{source_id}/{event_id}` | 多源日历查询、强制同步和写回 |
| `/api/todos`、`/api/habits`、`/api/countdowns`、`/api/focus` | 日常事项与专注统计 |
| `GET /api/rates`、`GET /api/rates/quote`、`GET /api/rates/history`、`/api/rates/pins` | 查询参考汇率与历史走势、添加和排序固定汇率对 |
| `GET / PATCH /api/ai/settings` & `POST /api/ai/chat` | AI 开关设置、SSE 流式对话与每日简报 |
| `GET / PUT / DELETE /api/wallpaper` | 跨设备共享壁纸管理 |

</details>

---

## 致谢（Credits）

本项目的实现离不开以下优秀的开源项目与开放服务：
- [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) — Windows 硬件传感器数据
- [AMLL (applemusic-like-lyrics)](https://github.com/amll-dev/applemusic-like-lyrics) — Apple Music 风格动态歌词渲染与 TTML/QRC 解析生态
- [LRCLIB](https://lrclib.net/) — 开源免费同步歌词库
- [Open-Meteo](https://open-meteo.com/) — 免 Key 开放天气与空气质量 API
- [TaskBarWidget](https://github.com/NoctisWang528/TaskBarWidget) — 多源歌词匹配与评分思路参考
- [FastAPI](https://fastapi.tiangolo.com/)、[React](https://react.dev/)、[Vite](https://vite.dev/)、[ECharts](https://echarts.apache.org/)、[Framer Motion](https://www.framer.com/motion/)、[PyWinRT](https://github.com/pywinrt/pywinrt)、[pycaw](https://github.com/AndreMiras/pycaw)

---

## License

本项目采用 [MIT License](LICENSE) 开源协议。
