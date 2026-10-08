"""The converter must match HA even when both schema libraries are installed."""

import runpy
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from tests.ha_fakes import install_homeassistant_fakes


@pytest.mark.parametrize("converter_name", ["convert", "to_openapi"])
def test_converter_follows_home_assistant_not_installed_packages(monkeypatch, converter_name):
    install_homeassistant_fakes(monkeypatch)
    llm = sys.modules["homeassistant.helpers.llm"]
    monkeypatch.delattr(llm, "convert")
    converter = Mock(return_value={"type": "object"})
    monkeypatch.setattr(llm, converter_name, converter, raising=False)
    for package, function in [("probatio", "to_openapi"), ("voluptuous_openapi", "convert")]:
        module = ModuleType(package)
        setattr(module, function, Mock(side_effect=AssertionError("Wrong converter selected")))
        monkeypatch.setitem(sys.modules, package, module)

    compat = runpy.run_path("custom_components/codex_assist/schema_compat.py")
    schema, serializer = object(), object()
    assert compat["to_openapi"](schema, custom_serializer=serializer) == {"type": "object"}
    converter.assert_called_once_with(schema, custom_serializer=serializer)


def test_converter_falls_back_to_probatio_when_helper_has_none(monkeypatch):
    install_homeassistant_fakes(monkeypatch)
    llm = sys.modules["homeassistant.helpers.llm"]
    monkeypatch.delattr(llm, "convert")
    monkeypatch.delattr(llm, "to_openapi", raising=False)
    converter = Mock(return_value={"type": "object"})
    module = ModuleType("probatio")
    module.to_openapi = converter
    monkeypatch.setitem(sys.modules, "probatio", module)

    compat = runpy.run_path("custom_components/codex_assist/schema_compat.py")
    schema, serializer = object(), object()
    assert compat["to_openapi"](schema, custom_serializer=serializer) == {"type": "object"}
    converter.assert_called_once_with(schema, custom_serializer=serializer)
