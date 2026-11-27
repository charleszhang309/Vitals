"""Local configuration and the HTTPS connection shared by the two CLIs."""

from dataclasses import dataclass
from ipaddress import ip_address
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

import httpx
import truststore

CONFIG_PATH = Path(__file__).with_name("config.json")
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PLACEHOLDER_TOKEN = "replace-with-windows-agents-token"


@dataclass(frozen=True)
class Config:
    server_url: str
    token: str
    device_id: str
    device_name: str
    reminders_list: str | None

    @classmethod
    def load(cls, path: Path, *, require_token: bool = True):
        with path.open(encoding="utf-8") as source:
            return cls.from_data(json.load(source), require_token=require_token)

    @classmethod
    def from_data(cls, data, *, require_token: bool = True):
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")

        server_url = data.get("server_url")
        if not isinstance(server_url, str):
            raise ValueError("请设置 server_url")
        url = urlsplit(server_url)
        if (not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path.rstrip("/") or "<" in server_url
                or not 0 < (url.port or 443) < 65536):
            raise ValueError("server_url 必须是无路径和凭证的服务地址")
        if url.scheme != "https" and not (url.scheme == "http" and is_loopback(url.hostname)):
            raise ValueError("server_url 必须使用 HTTPS，本机测试可使用回环 HTTP")

        token = data.get("token", "")
        if not isinstance(token, str) or (token and not re.fullmatch(r"[!-~]+", token)):
            raise ValueError("token 必须是无空白的 ASCII 字符串")
        if token == PLACEHOLDER_TOKEN or (require_token and not token):
            raise ValueError("请设置与 Windows agents.token 相同的 token")

        device_id = data.get("device_id", "macbook")
        device_name = data.get("device_name", "MacBook")
        reminders_list = data.get("reminders_list")
        if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
            raise ValueError("device_id 必须以英文字母或数字开头，只允许英文字母、数字、点、下划线和连字符，最多 64 位")
        if not isinstance(device_name, str) or not 1 <= len(device_name.strip()) <= 200:
            raise ValueError("device_name 必须包含 1–200 个字符")
        if reminders_list is not None and (not isinstance(reminders_list, str) or not reminders_list.strip()):
            raise ValueError("reminders_list 必须是列表名称或 null")
        return cls(server_url.rstrip("/"), token, device_id, device_name.strip(), reminders_list)

    def client(self, timeout: float | httpx.Timeout = 5):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.Client(
            base_url=self.server_url, headers=headers, timeout=timeout,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), trust_env=False,
        )


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
