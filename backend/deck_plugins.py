"""Load Deck plugins installed and enabled by the local administrator."""

from dataclasses import dataclass, field
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
import json
import logging
from math import isfinite
from pathlib import Path
import re
import sys
from threading import Lock
from types import ModuleType
from typing import Annotated, Iterable, Literal
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator


PLUGIN_ID_PATTERN = r"^(?:[a-z0-9][a-z0-9_-]{0,47}|_example)$"
MAX_MANIFEST_BYTES = 64 * 1024
logger = logging.getLogger(__name__)

PluginId = Annotated[str, StringConstraints(pattern=PLUGIN_ID_PATTERN)]
ItemId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")]
Locale = Annotated[str, StringConstraints(max_length=35, pattern=r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
LocalizedName = Annotated[dict[Locale, Label], Field(min_length=1, max_length=16)]
ValueType = Literal["string", "integer", "number", "boolean"]
Scalar = str | int | float | bool

_VALUE_TYPES = {"string": str, "integer": int, "boolean": bool}
_ERROR_MESSAGES = {
    "not_found": "Deck plugin item was not found.",
    "disabled": "This Deck plugin is disabled.",
    "invalid_parameters": "The Deck plugin parameters do not match the declared schema.",
    "unavailable": "This Deck plugin feature is unavailable.",
    "execution_failed": "The Deck plugin could not complete the request.",
}


class DeckPluginError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


def _matches_type(value: object, kind: ValueType) -> bool:
    if kind == "number":
        return type(value) is int or (type(value) is float and isfinite(value))
    return type(value) is _VALUE_TYPES[kind]


def _unique_ids(items):
    if len({item.id for item in items}) != len(items):
        raise ValueError("Duplicate Deck plugin item ID")
    return items


class _PluginModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, hide_input_in_errors=True)


class _Item(_PluginModel):
    id: ItemId
    name: LocalizedName


class _Parameter(_Item):
    type: ValueType
    required: bool = False
    default: Scalar | None = None
    choices: list[Scalar] | None = Field(default=None, min_length=1, max_length=48)

    def accepts(self, value: object) -> bool:
        return _matches_type(value, self.type) and (self.choices is None or value in self.choices)

    @model_validator(mode="after")
    def check_values(self):
        if self.choices is not None and (
            not all(_matches_type(choice, self.type) for choice in self.choices)
            or len(self.choices) != len(set(self.choices))
        ):
            raise ValueError("Deck plugin choices must be distinct values of the declared type")
        if "default" in self.model_fields_set and not self.accepts(self.default):
            raise ValueError("Deck plugin default does not match the declared schema")
        return self


class _ParameterizedItem(_Item):
    parameters: Annotated[list[_Parameter], Field(max_length=24), AfterValidator(_unique_ids)] = Field(default_factory=list)


class _Variable(_Item):
    type: ValueType


class _Manifest(_PluginModel):
    id: PluginId
    name: LocalizedName
    version: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    actions: Annotated[list[_ParameterizedItem], Field(max_length=64), AfterValidator(_unique_ids)] = Field(default_factory=list)
    feedbacks: Annotated[list[_ParameterizedItem], Field(max_length=64), AfterValidator(_unique_ids)] = Field(default_factory=list)
    variables: Annotated[list[_Variable], Field(max_length=64), AfterValidator(_unique_ids)] = Field(default_factory=list)


@dataclass
class _Plugin:
    directory: Path
    manifest: _Manifest
    enabled: bool
    lock: Lock = field(default_factory=Lock)
    module: ModuleType | None = None


class _PluginLoader(SourceFileLoader):
    def get_code(self, fullname: str):
        # Read the validated source directly without opening or creating a bytecode cache.
        return self.source_to_code(self.get_data(self.path), self.path)


def _verified_path(path: Path) -> bool:
    # The candidate starts under a canonical parent. Reject redirects through links or junctions.
    return path.resolve(strict=True) == path


def _unique_json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON member")
    return result


def _read_manifest(directory: Path) -> _Manifest:
    manifest_path = directory / "manifest.json"
    source_path = directory / "plugin.py"
    if not _verified_path(directory) or not all(
        _verified_path(path) and path.is_file() for path in (manifest_path, source_path)
    ):
        raise ValueError("Invalid Deck plugin files")
    with manifest_path.open("rb") as manifest_file:
        content = manifest_file.read(MAX_MANIFEST_BYTES + 1)
    if len(content) > MAX_MANIFEST_BYTES:
        raise ValueError("Deck plugin manifest is too large")
    manifest = _Manifest.model_validate(json.loads(content.decode("utf-8"), object_pairs_hook=_unique_json_object))
    if manifest.id != directory.name:
        raise ValueError("Deck plugin ID must match its directory name")
    return manifest


def _read_parameters(parameters: list[_Parameter], supplied: dict[str, object] | None) -> dict[str, Scalar]:
    supplied = {} if supplied is None else supplied
    declared = {parameter.id for parameter in parameters}
    if not isinstance(supplied, dict) or supplied.keys() - declared:
        raise DeckPluginError("invalid_parameters")

    validated = {}
    for parameter in parameters:
        if parameter.id in supplied:
            value = supplied[parameter.id]
            if not parameter.accepts(value):
                raise DeckPluginError("invalid_parameters")
            validated[parameter.id] = value
        elif parameter.default is not None:
            validated[parameter.id] = parameter.default
        elif parameter.required:
            raise DeckPluginError("invalid_parameters")
    return validated


def _load_module(plugin: _Plugin) -> ModuleType:
    if plugin.module is not None:
        return plugin.module

    path = plugin.directory / "plugin.py"
    name = f"_vitals_deck_plugin_{uuid4().hex}"
    try:
        if not _verified_path(plugin.directory) or not _verified_path(path) or not path.is_file():
            raise ValueError("Invalid Deck plugin source")
        loader = _PluginLoader(name, str(path))
        spec = spec_from_file_location(name, path, loader=loader)
        module = module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        if not callable(vars(module).get("execute_action")):
            raise ValueError("Missing required Deck plugin hook")
    except (Exception, SystemExit):
        sys.modules.pop(name, None)
        raise DeckPluginError("unavailable") from None

    plugin.module = module
    return module


def _call(plugin: _Plugin, method: str, *args) -> JsonValue:
    with plugin.lock:
        callback = vars(_load_module(plugin)).get(method)
        if not callable(callback):
            raise DeckPluginError("unavailable")
        try:
            result = callback(*args)
            # Keep a JSON snapshot while the plugin's state is locked and reject invalid Unicode or numbers.
            return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (Exception, SystemExit):
            raise DeckPluginError("execution_failed") from None


class DeckPluginRegistry:
    def __init__(self, directory: Path, enabled: Iterable[str] = ()):
        self._plugins: dict[str, _Plugin] = {}
        enabled_ids = frozenset(enabled)
        directory = Path(directory)
        try:
            directory = directory.parent.resolve() / directory.name
            if not _verified_path(directory):
                return
            candidates = sorted(directory.iterdir(), key=lambda path: path.name)
        except (OSError, RuntimeError, ValueError):
            return

        for candidate in candidates:
            if not re.fullmatch(PLUGIN_ID_PATTERN, candidate.name):
                continue
            try:
                manifest = _read_manifest(candidate)
            except (OSError, RuntimeError, ValueError):
                continue
            self._plugins[manifest.id] = _Plugin(candidate, manifest, manifest.id in enabled_ids)

    def load_enabled(self):
        for plugin in self._plugins.values():
            if plugin.enabled:
                with plugin.lock:
                    try:
                        _load_module(plugin)
                    except DeckPluginError:
                        logger.warning("Deck plugin unavailable at startup (%s)", plugin.manifest.id)

    def catalog(self) -> list[dict]:
        return [
            {**plugin.manifest.model_dump(mode="json", exclude_none=True), "enabled": plugin.enabled}
            for plugin in self._plugins.values()
        ]

    def _enabled(self, plugin_id: str) -> _Plugin:
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise DeckPluginError("not_found")
        if not plugin.enabled:
            raise DeckPluginError("disabled")
        return plugin

    def execute_action(self, plugin_id: str, action_id: str, params: dict[str, object] | None = None) -> JsonValue:
        plugin = self._enabled(plugin_id)
        action = next((item for item in plugin.manifest.actions if item.id == action_id), None)
        if action is None:
            raise DeckPluginError("not_found")
        return _call(plugin, "execute_action", action_id, _read_parameters(action.parameters, params))

    def get_state(self, plugin_id: str, feedback_id: str, params: dict[str, object] | None = None) -> JsonValue:
        plugin = self._enabled(plugin_id)
        feedback = next((item for item in plugin.manifest.feedbacks if item.id == feedback_id), None)
        if feedback is None:
            raise DeckPluginError("not_found")
        return _call(plugin, "get_state", feedback_id, _read_parameters(feedback.parameters, params))

    def get_variables(self, plugin_id: str) -> dict[str, Scalar]:
        plugin = self._enabled(plugin_id)
        declared = {variable.id: variable.type for variable in plugin.manifest.variables}
        if not declared:
            return {}
        result = _call(plugin, "get_variables")
        if not isinstance(result, dict) or any(
            key not in declared or not _matches_type(value, declared[key]) for key, value in result.items()
        ):
            raise DeckPluginError("execution_failed")
        return result
