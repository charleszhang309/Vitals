<div align="center">

# Vitals

**轻量级 Windows 个人状态台与跨设备控制台**  
集「今日概览（天气 + Apple Health）」「媒体与音量控制」「Windows 实时硬件监控」「跨设备图文/文件中转站」「可选 AI 助手」于一体，采用 Apple Health 极简卡片与 Liquid Glass（液态玻璃）设计。

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
- **天气与空气质量**：接入 Open-Meteo（免 Key），展示当前天气、US AQI、24 小时曲线与未来 7 天预报。
- **个人健康状态**：支持 **Health Auto Export v2 JSON** 双通道导入（iPhone REST 直推 + 本地同步目录），展示心率、活动能量、步数、睡眠 4 项主卡片、点击液态玻璃详情弹窗（今日每小时明细 + 7 天趋势），以及「7 天对比」卡片。
- **每日一句 AI 状态简报（可选）**：按本地自然日缓存，结合天气与健康数据生成简短提醒，支持 Bark 定时推送。

### 2. 控制中心
- **跨播放器媒体控制**：基于 Windows 原生 **SMTC**，支持 Apple Music、QQ 音乐、网易云音乐、Spotify 与主流浏览器，支持多会话切换与切歌/播放控制。
- **Apple Music 风格全屏播放 + AMLL 动态歌词**：点击「正在播放」进入全屏氛围界面，支持多源智能歌词匹配（按当前播放器优先选源，覆盖 AMLL TTML / QQ 音乐 / 酷狗 / 网易云 / LRCLIB）与 `@applemusic-like-lyrics/react` 动态渲染；无歌词时自动居中并隐藏歌词按钮。
- **系统音量与输出设备**：实时调节 Windows 主音量、一键静音及切换音频输出设备。

### 3. 硬件监控与远程控制
- **精准传感器数据**：优先读取本地 **LibreHardwareMonitor**（CPU / GPU / 内存 / 风扇温度、功耗、频率与转速），`psutil` 补充磁盘空间、网络速率、累计流量与**内存占用 Top 5 进程**，LHM 断开时自动兜底。
- **实时双模曲线**：支持「占用 / 温度」分段切换，CPU / GPU / 内存 / 网络 4 张主卡片均可点击查看深度详情（含各核心负载）。
- **白名单快捷控制**：电源计划切换（可选联动 FanControl）、一键锁屏、关闭显示器、30 / 60 分钟定时关机与取消。
- **任务检测与告警推送**：支持高负载任务完工检测、高温与低磁盘空间告警，通过 Bark 或通用 JSON Webhook 推送到手机。

### 4. 跨设备中转站
- 支持文本、图片、文件跨设备存取，`Ctrl+V` 直接粘贴截图，支持单卡片固定与定期自动清理。
- **纯前端文档预览**：液态玻璃弹窗直接预览 **PDF、DOCX、XLSX、CSV**（15 MB 以内，不依赖 LibreOffice 或云端服务）。

### 5. 个性化与移动端体验
- **Liquid Glass 液态玻璃**：支持浅色 / 深色主题、自定义跨设备共享壁纸、玻璃强度与遮罩透明度调节。
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
<summary>健康数据导入（Health Auto Export）与天气配置</summary>

- **天气**：在 `config.json` 中修改 `weather.city_name`、`latitude`、`longitude`（默认多伦多），缓存 20 分钟。
- **Health Auto Export 直推**：在 iPhone App 中新建 **REST API** 自动化，格式选 **JSON / Version 2**，URL 填 `https://<your-device>.<your-tailnet>.ts.net/api/health/ingest`（如配置了 `health.ingest_token`，添加 Header `Authorization: Bearer <token>`）。
- **本地目录同步**：将 `health.export_dir` 设为本地 Google Drive / iCloud 同步目录，服务每 2 分钟自动扫描去重导入。

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
| `POST /api/health/ingest` & `GET /api/health/...` | 导入 Health Auto Export v2 JSON 与查询健康摘要/详情 |
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
