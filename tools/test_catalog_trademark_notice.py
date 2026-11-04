"""The catalog keeps the source's trademark notice while product names remain client labels (R37)."""
import copy
import importlib.util
import json
from pathlib import Path

import jsonschema
import pytest

CERT = Path(__file__).resolve().parents[1] / "certification"
_SPEC = importlib.util.spec_from_file_location("trademark_notice_validator", CERT / "validate-protocol-requirements.py")
validator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validator)


def _load(name: str) -> dict:
    return json.loads((CERT / name).read_text(encoding="utf-8"))


def _roster() -> dict:
    return _load("sources/bounded-client-roster.v1.json")


def test_a_changed_catalog_notice_fails_closed():
    catalog = copy.deepcopy(_load("protocol-certification-requirements.v1.json"))
    catalog["trademarkNotice"] = "unrelated notice"
    with pytest.raises(ValueError, match="trademarkNotice"):
        validator.validate_desktop_clients(catalog, _roster())


def test_the_catalog_schema_requires_the_notice():
    catalog = copy.deepcopy(_load("protocol-certification-requirements.v1.json"))
    del catalog["trademarkNotice"]
    with pytest.raises(jsonschema.ValidationError, match="trademarkNotice"):
        jsonschema.validate(catalog, _load("protocol-certification-requirements.v1.schema.json"))
