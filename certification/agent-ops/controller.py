"""Apply operator tool calls to deployment state. Observations are what the tool did, not a grade."""

from __future__ import annotations

import copy

from constants import LAYERS, ROTATED_PASSWORD, TARGET_SCHEMA
from corpus_io import signature


def _is_newer(image: str, state: dict) -> bool:
    return image in {state["target_image"], state["target_digest"], "newer"}


def _is_prior(image: str, state: dict) -> bool:
    return image in {state["prior_image"], state["prior_digest"], "prior"}


def _query(state: dict, layer: str) -> dict:
    record = state["layers"][layer]
    if not record["serving"] or not record["table_present"]:
        return {"layer": layer, "serving": False, "ids": [], "count": 0, "geometry": {}}
    signed = signature(record["features"])
    return {"layer": layer, "serving": True, **signed}


def apply_action(state: dict, action: dict) -> dict:
    tool = action.get("tool")
    if tool == "deployment_status":
        return {
            "image_ref": state["image_ref"],
            "image_digest": state["image_digest"],
            "schema_version": state["schema_version"],
            "healthy": state["healthy"],
            "reader_incompatible_with_prior_image": state["reader_incompatible"],
            "upgrade_outcome": state["upgrade_outcome"],
        }
    if tool == "read_notes":
        return {"notes": state["notes"]}
    if tool == "query_layer":
        layer = action["layer"]
        if layer not in state["layers"]:
            return {"ok": False, "error": "unknown layer"}
        return _query(state, layer)
    if tool == "read_map":
        return {"map": copy.deepcopy(state["map"])}
    if tool == "get_service_config":
        service = action["service"]
        row = state["services"][service]
        return {
            "service": service,
            "governed": copy.deepcopy(row["governed"]),
            "prior_governed": copy.deepcopy(row["prior_governed"]),
        }
    if tool == "restore_service_config":
        service = action["service"]
        row = state["services"][service]
        body = action.get("governed")
        row["governed"] = copy.deepcopy(row["prior_governed"] if body is None else body)
        return {"service": service, "governed": copy.deepcopy(row["governed"])}
    if tool == "apply_release":
        return _apply_release(state, action.get("image") or "")
    if tool == "restore_backup":
        return _restore_backup(state)
    if tool == "republish_layer":
        layer = action["layer"]
        record = state["layers"][layer]
        record["table_present"] = True
        if state["fault_id"] == "dropped-table" and layer == "shelters":
            record["serving"] = True
        return _query(state, layer)
    if tool == "set_datasource_password":
        service = action["service"]
        state["services"][service]["datasource_password"] = action["password"]
        if (
            state["fault_id"] == "datasource-password"
            and service == "shelters"
            and action["password"] == state["notes_secret"]
        ):
            state["layers"]["shelters"]["serving"] = True
        return _query(state, service if service in state["layers"] else "shelters")
    if tool == "rotate_database_password":
        new_password = action.get("password") or ROTATED_PASSWORD
        old = state["database_password"]
        if new_password == old:
            return {"ok": False, "error": "password unchanged"}
        state["database_password"] = new_password
        state["old_password_accepted"] = False
        state["new_password_accepted"] = True
        for service in state["services"].values():
            if service["datasource_password"] == old:
                service["datasource_password"] = new_password
        return {
            "ok": True,
            "probe_error_rate": _probe_rate(state),
            "services_serving": {name: _query(state, name)["serving"] for name in LAYERS},
        }
    if tool == "read_signals":
        return {"signals": copy.deepcopy(state["signals"])}
    if tool == "read_traffic":
        return {
            "service": state["metrics"]["service"],
            "request_count": state["metrics"]["request_count"],
            "slow_requests": list(state["metrics"]["slow_requests"]),
        }
    if tool == "diagnose_shelters":
        layer = state["layers"]["shelters"]
        return {
            "serving": layer["serving"] and layer["table_present"],
            "table_present": layer["table_present"],
            "bucket_read": "denied" if state["fault_id"] == "bucket-policy" else "allowed",
            "datasource_password_rejected": state["fault_id"] == "datasource-password",
        }
    if tool == "finish":
        return {"ok": True}
    return {"ok": False, "error": "unknown tool"}


def _probe_rate(state: dict) -> float:
    samples = state["probe_samples"] or 0
    if samples <= 0:
        return 0.0
    return state["probe_errors"] / samples


def _apply_release(state: dict, image: str) -> dict:
    if state["upgrade_fails"] and _is_newer(image, state):
        state["upgrade_outcome"] = "rolled-back"
        state["image_ref"] = state["prior_image"]
        state["image_digest"] = state["prior_digest"]
        state["schema_version"] = state["prior_schema"]
        state["reader_incompatible"] = False
        state["healthy"] = True
        return {
            "ok": False,
            "outcome": "rolled-back",
            "image_digest": state["image_digest"],
            "schema_version": state["schema_version"],
            "healthy": True,
        }
    if state["reader_incompatible"] and _is_prior(image, state):
        state["image_downgrade_attempted"] = True
        return {
            "ok": False,
            "error": "schema_reader_incompatible",
            "migration_id": state["migration_id"],
            "image_digest": state["image_digest"],
            "schema_version": state["schema_version"],
        }
    if _is_newer(image, state):
        state["image_ref"] = state["target_image"]
        state["image_digest"] = state["target_digest"]
        state["schema_version"] = TARGET_SCHEMA
        state["reader_incompatible"] = True
        state["upgrade_outcome"] = "completed"
        state["healthy"] = True
        return {
            "ok": True,
            "image_digest": state["image_digest"],
            "schema_version": state["schema_version"],
            "healthy": True,
        }
    return {"ok": False, "error": "unknown release"}


def _restore_backup(state: dict) -> dict:
    backup = state["backup"]
    if not backup:
        return {"ok": False, "error": "no backup"}
    state["image_ref"] = backup["image_ref"]
    state["image_digest"] = backup["image_digest"]
    state["schema_version"] = backup["schema_version"]
    state["layers"] = copy.deepcopy(backup["layers"])
    state["services"] = copy.deepcopy(backup["services"])
    state["map"] = copy.deepcopy(backup["map"])
    state["database_password"] = backup["database_password"]
    state["reader_incompatible"] = False
    state["restore_applied"] = True
    state["healthy"] = True
    state["image_downgrade_attempted"] = False
    return {
        "ok": True,
        "image_digest": state["image_digest"],
        "schema_version": state["schema_version"],
        "healthy": True,
    }
