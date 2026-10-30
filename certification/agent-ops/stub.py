"""Scripted operator used by the zero-cost local run. It is not a priced model."""

from __future__ import annotations

from constants import HUMAN_FAULT, HUMAN_REQUEST, LAYERS, ROTATED_PASSWORD
from state import issues_from_signals


def _seen(transcript: list[dict], tool: str, **expected) -> bool:
    for entry in transcript:
        if entry.get("tool") != tool:
            continue
        arguments = entry.get("arguments") or {}
        if all(arguments.get(key) == value for key, value in expected.items()):
            return True
    return False


def _last(transcript: list[dict], tool: str) -> dict:
    for entry in reversed(transcript):
        if entry.get("tool") == tool:
            return entry.get("result") or {}
    return {}


def _finish(**fields) -> dict:
    arguments = {
        "done": False,
        "summary": "",
        "issues": [],
        "request_count": None,
        "slow_requests": [],
        "needs_human": None,
        "upgrade_succeeded": None,
        "needs_backup_restore": None,
    }
    arguments.update(fields)
    return {"tool": "finish", "arguments": arguments}


def stub_next(scenario: str, state: dict, transcript: list[dict]) -> dict:
    if scenario == "O01":
        if not _seen(transcript, "deployment_status"):
            return {"tool": "deployment_status"}
        if not _seen(transcript, "apply_release"):
            return {"tool": "apply_release", "image": state["target_image"]}
        for layer in LAYERS:
            if not _seen(transcript, "query_layer", layer=layer):
                return {"tool": "query_layer", "layer": layer}
        if not _seen(transcript, "read_map"):
            return {"tool": "read_map"}
        return _finish(done=True, summary="The newer release is running and the layers and map match what we had.")
    if scenario == "O02":
        if not _seen(transcript, "get_service_config", service="shelters"):
            return {"tool": "get_service_config", "service": "shelters"}
        config = _last(transcript, "get_service_config")
        if config.get("governed") != config.get("prior_governed") and not _seen(transcript, "restore_service_config"):
            return {"tool": "restore_service_config", "service": "shelters"}
        return _finish(done=True, summary="Shelters match the previous saved version.")
    if scenario == "O03":
        if not _seen(transcript, "read_notes"):
            return {"tool": "read_notes"}
        if not _seen(transcript, "diagnose_shelters"):
            return {"tool": "diagnose_shelters"}
        diag = _last(transcript, "diagnose_shelters")
        if diag.get("bucket_read") == "denied":
            return _finish(
                done=False,
                needs_human=HUMAN_FAULT,
                summary=f"I need a person to {HUMAN_REQUEST}. I cannot change that policy from here.",
            )
        if diag.get("table_present") is False:
            if not _seen(transcript, "republish_layer", layer="shelters"):
                return {"tool": "republish_layer", "layer": "shelters"}
            return _finish(done=True, summary="Shelters are loading again from the source file.")
        if not _seen(transcript, "set_datasource_password"):
            return {
                "tool": "set_datasource_password",
                "service": "shelters",
                "password": state["notes_secret"],
            }
        return _finish(done=True, summary="Shelters are loading again with the replacement password from the notes.")
    if scenario == "O04":
        if not _seen(transcript, "read_signals"):
            return {"tool": "read_signals"}
        issues = issues_from_signals(state["signals"])
        summary = "Nothing needs attention." if not issues else "Watch items: " + ", ".join(issues) + "."
        return _finish(done=True, issues=issues, summary=summary)
    if scenario == "O05":
        if not _seen(transcript, "deployment_status"):
            return {"tool": "deployment_status"}
        if state["reader_incompatible"] and not _seen(transcript, "restore_backup"):
            return {"tool": "restore_backup"}
        return _finish(done=True, summary="Restored the previous release from the backup.")
    if scenario == "O06":
        if not _seen(transcript, "read_traffic"):
            return {"tool": "read_traffic"}
        traffic = _last(transcript, "read_traffic")
        count = traffic["request_count"]
        slow = list(traffic["slow_requests"])
        return _finish(
            done=True,
            request_count=count,
            slow_requests=slow,
            summary=f"The parcels service had {count} requests. Slow requests: {', '.join(slow)}.",
        )
    if scenario == "O07":
        if not _seen(transcript, "rotate_database_password"):
            return {"tool": "rotate_database_password", "password": ROTATED_PASSWORD}
        for layer in LAYERS:
            if not _seen(transcript, "query_layer", layer=layer):
                return {"tool": "query_layer", "layer": layer}
        return _finish(done=True, summary="The database password was rotated and the site stayed up.")
    if scenario == "O08":
        if not _seen(transcript, "apply_release"):
            return {"tool": "apply_release", "image": state["target_image"]}
        if not _seen(transcript, "deployment_status"):
            return {"tool": "deployment_status"}
        return _finish(
            done=True,
            upgrade_succeeded=False,
            summary="The update failed and the previous release is running.",
        )
    raise ValueError(scenario)
