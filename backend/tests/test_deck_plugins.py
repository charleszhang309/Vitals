import json
from pathlib import Path
import sys

import pytest

from backend.deck_plugins import DeckPluginError, DeckPluginRegistry


MANIFEST = {
    "id": "counter",
    "name": {"en": "Counter", "zh-CN": "计数器"},
    "version": "1.0.0",
    "actions": [{
        "id": "add", "name": {"en": "Add"},
        "parameters": [
            {"id": "amount", "name": {"en": "Amount"}, "type": "integer", "required": True},
            {"id": "unit", "name": {"en": "Unit"}, "type": "string", "default": "points",
             "choices": ["points", "stars"]},
        ],
    }],
    "feedbacks": [{
        "id": "reached", "name": {"en": "Reached"},
        "parameters": [
            {"id": "threshold", "name": {"en": "Threshold"}, "type": "number", "default": 1},
        ],
    }],
    "variables": [{"id": "total", "name": {"en": "Total"}, "type": "integer"}],
}


def write_plugin(directory, manifest, source):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "plugin.py").write_text(source, encoding="utf-8")


def test_startup_loads_enabled_code_and_dispatch_validates_parameters(tmp_path):
    marker = tmp_path / "imported"
    write_plugin(tmp_path / "counter", MANIFEST, f"""\
from pathlib import Path
Path({str(marker)!r}).touch()

def execute_action(action_id, params):
    return params
""")
    disabled = DeckPluginRegistry(tmp_path)
    disabled.load_enabled()
    assert disabled.catalog()[0]["enabled"] is False
    with pytest.raises(DeckPluginError) as error:
        disabled.execute_action("counter", "add", {"amount": 3})
    assert error.value.code == "disabled"

    enabled = DeckPluginRegistry(tmp_path, enabled=["counter"])
    item, = enabled.catalog()
    assert item["enabled"] is True
    assert item["name"]["zh-CN"] == "计数器"
    assert item["actions"][0]["parameters"][1]["choices"] == ["points", "stars"]

    for invalid in ({}, {"amount": True}, {"amount": "3"}, {"amount": 3, "extra": 1},
                    {"amount": 3, "unit": "unknown"}, {"amount": 3, "unit": None}):
        with pytest.raises(DeckPluginError) as error:
            enabled.execute_action("counter", "add", invalid)
        assert error.value.code == "invalid_parameters"
    with pytest.raises(DeckPluginError) as error:
        enabled.execute_action("counter", "missing")
    assert error.value.code == "not_found"
    assert not marker.exists()

    enabled.load_enabled()
    assert marker.exists()
    assert enabled.execute_action("counter", "add", {"amount": 3}) == {"amount": 3, "unit": "points"}


def test_feedback_and_variables_share_plugin_state(tmp_path):
    write_plugin(tmp_path / "counter", MANIFEST, """\
total = 0

def execute_action(action_id, params):
    global total
    total += params['amount']

def get_state(feedback_id, params):
    return total >= params['threshold']

def get_variables():
    return {'total': total}
""")
    registry = DeckPluginRegistry(tmp_path, enabled=["counter"])
    assert registry.get_state("counter", "reached") is False
    assert registry.execute_action("counter", "add", {"amount": 3}) is None
    assert registry.get_state("counter", "reached") is True
    assert registry.get_state("counter", "reached", {"threshold": 3.1}) is False
    assert registry.get_variables("counter") == {"total": 3}


@pytest.mark.parametrize("overrides", [
    {"id": "../counter"},
    {"id": "another"},
    {"entrypoint": "../worker.py"},
    {"actions": [MANIFEST["actions"][0], MANIFEST["actions"][0]]},
    {"actions": [{"id": "add", "name": {"en": "Add"}, "parameters": [
        {"id": "amount", "name": {"en": "Amount"}, "type": "integer", "default": True},
    ]}]},
])
def test_invalid_manifests_are_excluded(tmp_path, overrides):
    write_plugin(tmp_path / "counter", {**MANIFEST, **overrides}, "")
    registry = DeckPluginRegistry(tmp_path, enabled=["counter"])
    assert registry.catalog() == []
    with pytest.raises(DeckPluginError) as error:
        registry.execute_action("counter", "add", {"amount": 3})
    assert error.value.code == "not_found"


def test_example_plugin_requires_explicit_enablement():
    directory = Path(__file__).resolve().parents[2] / "plugins"
    registry = DeckPluginRegistry(directory)
    example = next(item for item in registry.catalog() if item["id"] == "_example")
    assert example["enabled"] is False
    with pytest.raises(DeckPluginError) as error:
        registry.execute_action("_example", "increment")
    assert error.value.code == "disabled"
    enabled = DeckPluginRegistry(directory, enabled=["_example"])
    assert enabled.execute_action("_example", "increment") == {"count": 1}
    assert enabled.get_state("_example", "at-target", {"target": 1}) is True
    assert enabled.get_variables("_example") == {"count": 1}


def test_discovery_skips_external_plugin_directories(tmp_path):
    directory = tmp_path / "plugins"
    directory.mkdir()
    external = tmp_path / "outside" / "counter"
    write_plugin(external, MANIFEST, "")
    link = directory / "counter"
    if sys.platform == "win32":
        # NTFS directory junctions can be created without elevation.
        from _winapi import CreateJunction

        CreateJunction(str(external), str(link))
    else:
        link.symlink_to(external, target_is_directory=True)
    assert DeckPluginRegistry(directory, enabled=["counter"]).catalog() == []


def test_plugin_errors_and_variable_output_do_not_expose_internal_data(tmp_path):
    write_plugin(tmp_path / "counter", MANIFEST, """\
def execute_action(action_id, params):
    raise RuntimeError('private path or credential')

def get_variables():
    return {'total': True, 'secret': 'private path or credential'}
""")
    registry = DeckPluginRegistry(tmp_path, enabled=["counter"])
    with pytest.raises(DeckPluginError) as error:
        registry.execute_action("counter", "add", {"amount": 3})
    assert error.value.code == "execution_failed"
    assert "private" not in str(error.value)
    with pytest.raises(DeckPluginError) as error:
        registry.get_state("counter", "reached")
    assert error.value.code == "unavailable"
    with pytest.raises(DeckPluginError) as error:
        registry.get_variables("counter")
    assert error.value.code == "execution_failed"
