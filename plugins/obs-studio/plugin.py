"""OBS Studio controls over the built-in obs-websocket v5 JSON protocol."""

from base64 import b64encode
from hashlib import sha256
from inspect import signature
from ipaddress import IPv6Address
import json
from pathlib import Path
import re
from time import monotonic
from uuid import uuid4

from websockets.exceptions import WebSocketException
from websockets.protocol import State

try:
    from websockets.sync.client import connect
except ImportError:
    connect = None


CONFIG_PATH = Path(__file__).with_name("config.json")
_configured = {}
CONNECT_OPTIONS = {"proxy": None} if connect and "proxy" in signature(connect).parameters else {}
REQUEST_TIMEOUT = 1.5
SNAPSHOT_TTL = 0.5
RECONNECT_DELAY = 2.0
TRANSPORT_ERRORS = (OSError, ValueError, WebSocketException)
OUTPUT_REQUESTS = {
    "start-recording": ("StartRecord", 500),
    "stop-recording": ("StopRecord", 501),
    "start-streaming": ("StartStream", 500),
    "stop-streaming": ("StopStream", 501),
}
STATUS_REQUESTS = {
    "recording": "GetRecordStatus",
    "streaming": "GetStreamStatus",
    "current-scene": "GetCurrentProgramScene",
}
OFFLINE_MESSAGE = (
    "无法连接 OBS。请启动 OBS 并检查 WebSocket 服务器及连接设置。 / "
    "Cannot connect to OBS. Start OBS and check its WebSocket server and connection settings."
)


class InvalidConfig(ValueError):
    pass


class RequestRejected(Exception):
    def __init__(self, code):
        self.code = code


def _connection_settings(overrides=None):
    overrides = _configured if overrides is None else overrides
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError):
        if not overrides:
            raise InvalidConfig from None
        config = {}
    if not isinstance(config, dict):
        if not overrides:
            raise InvalidConfig
        config = {}
    config.update(overrides)

    host = config.get("host", "127.0.0.1")
    port = config.get("port", 4455)
    password = config.get("password", "")
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535 or not isinstance(password, str):
        raise InvalidConfig
    if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        try:
            host = f"[{IPv6Address(host.removeprefix('[').removesuffix(']'))}]"
        except ValueError:
            raise InvalidConfig from None
    return f"ws://{host}:{port}", password


def _authentication(password, salt, challenge):
    secret = b64encode(sha256((password + salt).encode("utf-8")).digest())
    return b64encode(sha256(secret + challenge.encode("utf-8")).digest()).decode("ascii")


def _response_data(message, request_type):
    if not isinstance(message, dict) or message.get("requestType") != request_type:
        raise ValueError("Unexpected OBS response type")
    status = message.get("requestStatus")
    if not isinstance(status, dict) or type(status.get("result")) is not bool or type(status.get("code")) is not int:
        raise ValueError("Invalid OBS request status")
    if not status["result"]:
        raise RequestRejected(status["code"])
    data = message.get("responseData", {})
    if not isinstance(data, dict):
        raise ValueError("Invalid OBS response data")
    return data


def _scene_name(data):
    # obs-websocket 5.0 used currentProgramSceneName. Newer versions also send sceneName.
    name = data.get("sceneName", data.get("currentProgramSceneName"))
    if not isinstance(name, str):
        raise ValueError("Invalid OBS scene name")
    return name


class OBSClient:
    def __init__(self):
        self.socket = None
        self.snapshot = None
        self.refresh_at = 0.0

    def disconnect(self):
        socket, self.socket = self.socket, None
        self.snapshot = None
        self.refresh_at = monotonic() + RECONNECT_DELAY
        if socket is not None:
            try:
                socket.close()
            except TRANSPORT_ERRORS:
                pass

    def _receive(self, opcode, deadline):
        message = json.loads(self.socket.recv(timeout=max(0, deadline - monotonic())))
        if not isinstance(message, dict) or message.get("op") != opcode or not isinstance(message.get("d"), dict):
            raise ValueError("Unexpected OBS protocol message")
        return message["d"]

    def _send(self, opcode, data):
        self.socket.send(json.dumps({"op": opcode, "d": data}))

    def _connect(self, deadline):
        if self.socket is not None:
            if self.socket.state is State.OPEN:
                return
            self.disconnect()
        if connect is None:
            raise OSError("WebSocket client unavailable")

        uri, password = _connection_settings()
        self.socket = connect(uri, open_timeout=max(0, deadline - monotonic()), close_timeout=0.2,
                              max_size=256 * 1024, compression=None, **CONNECT_OPTIONS)
        hello = self._receive(0, deadline)
        if type(hello.get("rpcVersion")) is not int or hello["rpcVersion"] < 1:
            raise ValueError("Unsupported OBS RPC version")
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        if "authentication" in hello:
            auth = hello["authentication"]
            if not isinstance(auth, dict) or not all(isinstance(auth.get(field), str) for field in ("salt", "challenge")):
                raise ValueError("Invalid OBS authentication challenge")
            identify["authentication"] = _authentication(password, auth["salt"], auth["challenge"])
        self._send(1, identify)
        if self._receive(2, deadline).get("negotiatedRpcVersion") != 1:
            raise ValueError("Unsupported OBS RPC version")

    def _exchange(self, opcode, data, deadline):
        self._connect(deadline)
        if monotonic() >= deadline:
            raise TimeoutError
        request_id = uuid4().hex
        self._send(opcode, {"requestId": request_id, **data})
        response = self._receive(opcode + 1, deadline)
        if response.get("requestId") != request_id:
            raise ValueError("Unexpected OBS request ID")
        return response

    def request(self, request_type, deadline, **data):
        body = {"requestType": request_type}
        if data:
            body["requestData"] = data
        return _response_data(self._exchange(6, body, deadline), request_type)

    def status(self):
        if monotonic() < self.refresh_at:
            return self.snapshot

        try:
            response = self._exchange(8, {
                "haltOnFailure": False,
                "requests": [{"requestType": name} for name in STATUS_REQUESTS.values()],
            }, monotonic() + REQUEST_TIMEOUT)
            results = response.get("results")
            if not isinstance(results, list) or len(results) != len(STATUS_REQUESTS):
                raise ValueError("Invalid OBS batch response")
            snapshot = {}
            for (key, request_type), result in zip(STATUS_REQUESTS.items(), results):
                try:
                    data = _response_data(result, request_type)
                except RequestRejected:
                    continue
                if key == "current-scene":
                    snapshot[key] = _scene_name(data)
                elif type(data.get("outputActive")) is bool:
                    snapshot[key] = data["outputActive"]
        except TRANSPORT_ERRORS:
            self.disconnect()
            return None

        self.snapshot = snapshot
        self.refresh_at = monotonic() + SNAPSHOT_TTL
        return snapshot


_client = OBSClient()


def configure(settings):
    global _configured
    if settings:
        _connection_settings(settings)
    if _configured != settings:
        _configured = dict(settings)
        _client.disconnect()
        _client.refresh_at = 0.0


def shutdown():
    _client.disconnect()


def execute_action(action_id, params):
    deadline = monotonic() + REQUEST_TIMEOUT
    try:
        if action_id in OUTPUT_REQUESTS:
            request_type, already_set = OUTPUT_REQUESTS[action_id]
            try:
                _client.request(request_type, deadline)
            except RequestRejected as error:
                if error.code != already_set:
                    raise
        elif action_id == "switch-scene":
            _client.request("SetCurrentProgramScene", deadline, sceneName=params["scene_name"])
        elif action_id in {"mute-audio", "unmute-audio"}:
            _client.request("SetInputMute", deadline, inputName=params["input_name"], inputMuted=action_id == "mute-audio")
        else:  # Show or hide a scene source, as validated by the manifest.
            scene = params.get("scene_name")
            if not scene:
                scene = _scene_name(_client.request("GetCurrentProgramScene", deadline))
            item = _client.request("GetSceneItemId", deadline, sceneName=scene, sourceName=params["source_name"])
            item_id = item.get("sceneItemId")
            if type(item_id) is not int:
                raise ValueError("Invalid OBS scene item ID")
            _client.request("SetSceneItemEnabled", deadline, sceneName=scene, sceneItemId=item_id,
                            sceneItemEnabled=action_id == "show-source")
    except RequestRejected as error:
        message = ("找不到 OBS 场景或源，请检查名称。 / Check the OBS scene or source name."
                   if error.code == 600 else "OBS 拒绝了该请求。 / OBS rejected the request.")
        return {"ok": False, "code": error.code, "message": message}
    except InvalidConfig:
        return {"ok": False, "message": "请检查 Deck 插件管理中的 OBS 连接设置。 / Check the OBS connection in Deck Plugins."}
    except TRANSPORT_ERRORS:
        _client.disconnect()
        return {"ok": False, "message": OFFLINE_MESSAGE}

    _client.refresh_at = 0.0
    return {"ok": True}


def get_state(feedback_id, params):
    snapshot = _client.status()
    if feedback_id == "connection":
        return {"active": snapshot is not None, "value": "在线 / Online" if snapshot is not None else "离线 / Offline"}
    value = snapshot.get(feedback_id) if snapshot is not None else None
    if feedback_id == "recording" and value is not None:
        return {"active": value, "value": "🔴" if value else "○"}
    return value


def get_variables():
    return _client.status() or {}
