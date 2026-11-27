"""从项目目录加载并校验本机配置。"""

import ipaddress
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from string import Formatter
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, TypeAdapter, field_validator, model_validator


ROOT = Path(__file__).resolve().parent.parent
MIB = 1024**2
GIB = 1024**3
OPENAI_MODEL = "gpt-6-luna"
OPENAI_HOSTS = {"api.openai.com", "us.api.openai.com", "eu.api.openai.com"}
SamplingMode = Literal["smart", "realtime", "eco"]
LAUNCHER_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$"
_config_write_lock = Lock()


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class LhmConfig(ConfigModel):
    url: HttpUrl = HttpUrl("http://localhost:8085/data.json")
    timeout_seconds: float = Field(default=1.0, gt=0, le=1.5)
    fan_mapping: dict[str, Literal["cpu", "gpu", "chassis", "pump"]] = Field(default_factory=dict)


class TaskConfig(ConfigModel):
    source: Literal["auto", "cpu", "gpu"] = "auto"
    high_percent: float = Field(default=70, ge=0, lt=100)
    high_seconds: float = Field(default=300, gt=0)
    low_percent: float = Field(default=10, gt=0, le=100)
    low_seconds: float = Field(default=120, gt=0)

    @model_validator(mode="after")
    def check_thresholds(self):
        if self.low_percent >= self.high_percent:
            raise ValueError("low_percent 必须小于 high_percent")
        return self


class AlertConfig(ConfigModel):
    cpu_temperature_c: float = Field(default=90, gt=0)
    gpu_temperature_c: float = Field(default=90, gt=0)
    temperature_seconds: float = Field(default=30, gt=0)
    disk_free_gib: float = Field(default=15, ge=0)
    cooldown_seconds: float = Field(default=1800, gt=0)


class WebhookConfig(ConfigModel):
    kind: Literal["disabled", "bark", "json"] = "disabled"
    bark_url_template: str | None = None
    json_url: HttpUrl | None = None
    timeout_seconds: float = Field(default=5, gt=0, le=30)
    daily_briefing_time: str | None = Field(default=None, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")

    @model_validator(mode="after")
    def check_destination(self):
        if self.kind == "bark":
            if not self.bark_url_template:
                raise ValueError("启用 Bark 时必须填写 bark_url_template")
            fields = set()
            for _, name, spec, conversion in Formatter().parse(self.bark_url_template):
                if name is None:
                    continue
                if name not in {"title", "body"} or spec or conversion:
                    raise ValueError("Bark 模板仅支持 {title} 和 {body}")
                fields.add(name)
            if fields != {"title", "body"}:
                raise ValueError("Bark 模板必须包含 {title} 和 {body}")
            TypeAdapter(HttpUrl).validate_python(self.bark_url_template.format(title="title", body="body"))
        if self.kind == "json" and self.json_url is None:
            raise ValueError("启用 JSON Webhook 时必须填写 json_url")
        return self


class PowerMode(ConfigModel):
    guid: UUID
    fan_profile: Path | None = None

    @field_validator("fan_profile")
    @classmethod
    def check_profile(cls, path: Path | None):
        if path is not None and path.suffix.lower() != ".json":
            raise ValueError("风扇配置文件必须是 .json")
        return path


class WeatherConfig(ConfigModel):
    city_name: str = Field(default="伦敦", min_length=1, max_length=100)
    latitude: float = Field(default=51.5074, ge=-90, le=90)
    longitude: float = Field(default=-0.1278, ge=-180, le=180)


class HealthConfig(ConfigModel):
    export_dir: Path | None = None
    timezone: str = "UTC"
    ingest_token: SecretStr | None = None

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("使用有效的 IANA 时区，例如 Europe/London") from error
        return value


class AgentsConfig(ConfigModel):
    token: SecretStr | None = None


class CalendarSource(ConfigModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    name: str = Field(min_length=1, max_length=80)
    type: Literal["icloud", "google", "outlook", "ics"]
    color: str = Field(default="#007AFF", pattern=r"^#[0-9a-fA-F]{6}$")
    writable: bool = True
    calendar_id: str | None = Field(default=None, max_length=400)
    apple_id: SecretStr | None = None
    app_password: SecretStr | None = None
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    refresh_token: SecretStr | None = None
    tenant: str = Field(default="common", pattern=r"^[a-zA-Z0-9.-]{1,100}$")
    url: SecretStr | None = None

    @property
    def can_write(self):
        return self.writable and self.type != "ics"


class CalendarConfig(ConfigModel):
    sources: list[CalendarSource] = Field(default_factory=list, max_length=16)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, sources):
        if len({source.id for source in sources}) != len(sources):
            raise ValueError("日历来源 ID 不能重复")
        return sources


class FinanceConfig(ConfigModel):
    base_currency: str = "CAD"
    target_currencies: list[str] = Field(default_factory=lambda: ["CNY", "USD"], min_length=1, max_length=12)

    @field_validator("base_currency")
    @classmethod
    def currency(cls, value):
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("请使用三位币种代码")
        return value

    @field_validator("target_currencies")
    @classmethod
    def currencies(cls, values):
        return list(dict.fromkeys(cls.currency(value) for value in values))


class AiConfig(ConfigModel):
    enabled: bool = False
    base_url: HttpUrl = HttpUrl("https://api.openai.com/v1")
    api_key: SecretStr | None = None
    model: str = Field(default=OPENAI_MODEL, min_length=1, max_length=100, pattern=r"^\S+$")
    include_vitals_context: bool = True

    @field_validator("base_url")
    @classmethod
    def check_base_url(cls, value: HttpUrl):
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("AI base_url 只能包含服务地址和路径")
        return value

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, value):
        return value.strip() if isinstance(value, str) else value

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_key.get_secret_value().strip()
                    and self.base_url.scheme == "https" and self.base_url.host in OPENAI_HOSTS
                    and self.base_url.port in {None, 443} and (self.base_url.path or "").rstrip("/") == "/v1")

    @property
    def available(self) -> bool:
        return self.enabled and self.configured


class AiUsageConfig(ConfigModel):
    deepseek_api_key: SecretStr | None = None
    openai_admin_key: SecretStr | None = None
    anthropic_admin_key: SecretStr | None = None


class Launcher(ConfigModel):
    id: str = Field(pattern=LAUNCHER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["app", "url"]
    target: str = Field(min_length=1, max_length=2048)

    @field_validator("name", "target", mode="before")
    @classmethod
    def normalize_text(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("启动项不可包含控制字符")
        return value

    @model_validator(mode="after")
    def check_target(self):
        if self.kind == "url":
            url = HttpUrl(self.target)
            if url.username or url.password:
                raise ValueError("启动链接不可包含用户名或密码")
            self.target = str(url)
        else:
            windows = PureWindowsPath(self.target)
            mac = PurePosixPath(self.target)
            windows_ok = (windows.is_absolute() and re.fullmatch(r"[A-Za-z]:", windows.drive)
                          and windows.suffix.lower() == ".exe" and ".." not in windows.parts
                          and not any(set(part) & set('<>:"|?*') for part in windows.parts[1:]))
            mac_ok = (mac.anchor == "/" and mac.suffix == ".app" and ".." not in mac.parts and "\\" not in self.target)
            if not (windows_ok or mac_ok):
                raise ValueError("应用启动项必须指定本地 .exe 或 .app 的绝对路径")
        return self


class DeckWebhook(ConfigModel):
    id: str = Field(pattern=LAUNCHER_ID_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    url: HttpUrl
    method: Literal["GET", "POST"] = "POST"

    @model_validator(mode="after")
    def validate_url(self):
        if self.url.username or self.url.password or self.url.fragment:
            raise ValueError("Deck webhook URL 不可包含用户凭据或片段")
        return self


class DeckConfig(ConfigModel):
    enabled_plugins: list[str] = Field(default_factory=list, max_length=64)
    webhooks: list[DeckWebhook] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_entries(self):
        if any(not re.fullmatch(r"(?:[a-z0-9][a-z0-9_-]{0,47}|_example)", item) for item in self.enabled_plugins):
            raise ValueError("Deck 插件 ID 无效")
        if len({item.id for item in self.webhooks}) != len(self.webhooks):
            raise ValueError("Deck webhook ID 不可重复")
        return self


def default_modes() -> dict[str, PowerMode]:
    return {
        "silent": PowerMode(guid=UUID("a1841308-3541-4fab-bc81-f71556f20b4a")),
        "balanced": PowerMode(guid=UUID("381b4222-f694-41f0-9685-ff5bb260df2e")),
        "performance": PowerMode(guid=UUID("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c")),
    }


class Settings(ConfigModel):
    # 仅绑定 Tailscale 接口时，将 host 改为本机的 Tailscale IP。
    host: str = "0.0.0.0"
    port: int = Field(default=3993, strict=True, ge=1, le=65535)
    upload_limit_mib: int = Field(default=50, strict=True, gt=0)
    sampling_mode: SamplingMode = "smart"
    lhm: LhmConfig = Field(default_factory=LhmConfig)
    task: TaskConfig = Field(default_factory=TaskConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    finance: FinanceConfig = Field(default_factory=FinanceConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    ai_usage: AiUsageConfig = Field(default_factory=AiUsageConfig)
    launchers: list[Launcher] = Field(default_factory=list, max_length=32)
    deck: DeckConfig = Field(default_factory=DeckConfig)
    fancontrol_mode_sensor: bool = False
    fancontrol_exe: Path | None = None
    power_modes: dict[str, PowerMode] = Field(default_factory=default_modes)

    @field_validator("host")
    @classmethod
    def check_host(cls, host: str):
        if host != "localhost":
            ipaddress.ip_address(host)
        return host

    @field_validator("launchers")
    @classmethod
    def unique_launcher_ids(cls, launchers: list[Launcher]) -> list[Launcher]:
        if len({launcher.id for launcher in launchers}) != len(launchers):
            raise ValueError("启动项 ID 不可重复")
        return launchers

    @field_validator("fancontrol_exe")
    @classmethod
    def check_exe(cls, path: Path | None):
        if path is not None and path.suffix.lower() != ".exe":
            raise ValueError("fancontrol_exe 必须指向 .exe 文件")
        return path

    @field_validator("power_modes")
    @classmethod
    def check_mode_names(cls, modes: dict[str, PowerMode]):
        for name in modes:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
                raise ValueError("模式名须以小写英文字母开头，最多 32 个字符")
        return modes


def load_settings(path: Path = ROOT / "config.json") -> Settings:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return Settings()
    return Settings.model_validate_json(raw)


def update_settings_file(path: Path, update) -> Settings:
    with _config_write_lock:
        return _update_settings_file(path, update)


def _update_settings_file(path: Path, update) -> Settings:
    original = path.read_bytes() if path.exists() else None
    document = json.loads(original) if original is not None else {}
    previous = Settings.model_validate(document)
    update(document, previous)
    result = Settings.model_validate(document)
    if result == previous:
        return result
    pending = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise OSError("配置文件已被其他进程更新")
        pending.replace(path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return result


def save_ai_preferences(path: Path, updates: dict[str, bool]) -> AiConfig:
    preferences = {name: value for name, value in updates.items()
                   if name in {"enabled", "include_vitals_context"}}
    return update_settings_file(path, lambda document, _: document.setdefault("ai", {}).update(preferences)).ai
