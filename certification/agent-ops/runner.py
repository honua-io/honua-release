"""Run seeded operate scenarios and print one v2 summary line.

The scripted operator is the local check. A live model call needs --model live and --priced-ok,
and this lane does not pass those flags.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from blinding import assert_clean
from catalog import advertised_tools
from constants import (
    ARMS,
    GUIDANCE,
    HUMAN_REVIEW,
    JUDGE,
    MAX_ACTIONS,
    MAX_CONTEXT_TOKENS,
    NEWER_IMAGE,
    NOTES_SECRET,
    PILOT_IDS,
    PRICES_USD_PER_MTOK,
    PRIOR_IMAGE,
    ROTATED_PASSWORD,
    SHIPPED_TEMPERATURE,
    SPEND_CAP_USD,
)
from controller import apply_action
from fixture import ENV_NAMES, PROCESS_NAMES, write_fixture
from oracles import infrastructure_evidence
from scenarios import is_held_out_row, load_prompts, preamble, retry_prompt
from score import preflight, stop_reasons, summarize, summary_line
from state import INITIAL_PASSWORD, build_case
from stub import stub_next
from targets import aws_ecs_plan, local_docker_plan, prepare_images, start_deployment, stop_deployment

HERE = Path(__file__).resolve().parent
DEFAULT_ROWS = HERE / "rows" / "development.jsonl"


def _read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001")), "f")


def _usage_pair(usage: object) -> tuple[int, int] | None:
    if not isinstance(usage, dict):
        return None
    incoming = usage.get("prompt_tokens", usage.get("promptTokens"))
    outgoing = usage.get("completion_tokens", usage.get("completionTokens"))
    if type(incoming) is int and type(outgoing) is int and incoming >= 0 and outgoing >= 0:
        return incoming, outgoing
    return None


def _trial_cost(arm: str, incoming: int, outgoing: int) -> str:
    prices = PRICES_USD_PER_MTOK[arm]
    total = (
        Decimal(incoming) * Decimal(prices["input"]) + Decimal(outgoing) * Decimal(prices["output"])
    ) / Decimal(1_000_000)
    return _money(total)


def _load_ledger(path: Path | None, cap: Decimal) -> dict | None:
    if path is None:
        return None
    if path.is_file():
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise RuntimeError("ledger is not an object")
    else:
        document = {}
    document["cap_usd"] = _money(cap)
    document.setdefault("spent_usd", _money(Decimal(0)))
    document.setdefault("status", "open")
    if not isinstance(document.get("trials"), list):
        document["trials"] = []
    return document


def _ledger_status(document: dict | None) -> str | None:
    if document is None:
        return None
    status = document.get("status")
    return status if isinstance(status, str) else None


def _exhausted(document: dict | None) -> bool:
    if document is None:
        return False
    if document.get("status") in {"budget_exhausted", "usage_missing"}:
        return True
    try:
        spent = Decimal(str(document.get("spent_usd") or "0"))
        cap = Decimal(str(document.get("cap_usd")))
    except Exception:
        return True
    return spent >= cap


def _write_ledger(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def _pilot_block(path: Path | None, *, pilot: bool, arm: str) -> str | None:
    """A full run may start only after a priced live pilot of the same arm."""
    if pilot:
        return None
    if path is None:
        return "a full run needs the pilot summary from a named prior run"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "the pilot summary could not be read"
    if not isinstance(document, dict):
        return "the pilot summary is not an object"
    if document.get("scope") != "pilot":
        return "the prior summary is not a pilot"
    if document.get("driver") != "live":
        return "the prior summary is not a live run"
    if document.get("priced") is not True:
        return "the prior summary did not make a priced call"
    if document.get("arm") != arm:
        return "the prior summary used a different arm"
    if document.get("n") != len(PILOT_IDS):
        return "the prior summary is not the three pilot trials"
    stop = document.get("stop")
    reasons = stop.get("reasons") if isinstance(stop, dict) else None
    if not isinstance(reasons, list) or any(reasons):
        return "the pilot summary has stop reasons"
    if document.get("status") != "complete":
        return "the pilot summary did not complete"
    return None


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _arguments(action: dict) -> dict:
    if isinstance(action.get("arguments"), dict):
        return dict(action["arguments"])
    return {key: value for key, value in action.items() if key not in {"tool", "kind", "report", "text"}}


def _measure(prompt: str, transcript: list[dict]) -> dict:
    report = ""
    tool_blob = []
    for item in transcript:
        if item.get("kind") == "report":
            report = item.get("text") or ""
        else:
            tool_blob.append(item)
    input_tokens = _tokens(preamble() + prompt + json.dumps(tool_blob, default=str))
    output_tokens = _tokens(report)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "context_tokens": input_tokens + output_tokens,
    }


def _drive_stub(scenario: str, state: dict, limit: int) -> tuple[list[dict], bool]:
    transcript: list[dict] = []
    for _ in range(limit):
        action = stub_next(scenario, state, transcript)
        if action.get("kind") == "report":
            transcript.append(action)
            return transcript, False
        result = apply_action(state, action)
        transcript.append({"tool": action["tool"], "arguments": _arguments(action), "result": result})
    return transcript, True


def _tool_action(kind: str, action: dict) -> dict:
    if kind == "tool_call":
        return {"tool": action["tool"], "arguments": dict(action.get("arguments") or {})}
    return {"tool": "terminal_command", "arguments": {"command": action.get("command", "")}}


def _endpoint_block(arm_id: str) -> str | None:
    """Refuse a live run before any trial when the Studio AI proxy is not configured."""
    from client import CanaryError, build_endpoint

    endpoint = build_endpoint(arm_id)
    if not getattr(endpoint, "configured", False):
        return "the model endpoint is unset"
    if getattr(endpoint, "require_api_key", False) and not getattr(endpoint, "api_key", None):
        return "the model endpoint has no admin API key"
    if getattr(endpoint, "require_api_key", False):
        digest = str(getattr(endpoint, "signing_manifest_sha256", "") or "").lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            return "the transcript signing manifest is unset"
    try:
        if hasattr(endpoint, "proxy_chat_url"):
            endpoint.proxy_chat_url()
    except CanaryError as exc:
        return str(exc)
    return None


def live_actions(
    state: dict,
    transcript: list[dict],
    row: dict,
    arm: str,
    limit: int,
    *,
    allow_call,
    on_usage,
    wait_for_server: bool = False,
) -> tuple[bool, bool, dict | None]:
    """Call the canary client. Returns (priced, completed, evidence)."""
    raise RuntimeError("not-real: live operations and independent readback are not implemented")
    from client import (
        CanaryError,
        ModelFailure,
        OperateClient,
        build_endpoint,
        parse_model_action,
        server_ready,
    )

    endpoint = build_endpoint(arm)
    if not endpoint.configured:
        return False, False, {
            "kind": "transport_error",
            "replay": "endpoint-unconfigured",
            "model_acted": False,
            "state_changed": False,
        }
    if wait_for_server and not server_ready(endpoint):
        return False, False, {
            "kind": "transport_error",
            "replay": "server-not-ready",
            "model_acted": False,
            "state_changed": False,
        }
    client = OperateClient(endpoint)
    if hasattr(client, "ensure_admission"):
        try:
            client.ensure_admission()
        except (CanaryError, ModelFailure, OSError) as exc:
            return False, False, {
                "kind": "transport_error",
                "replay": type(exc).__name__,
                "detail": _error_detail(exc, state),
                "model_acted": False,
                "state_changed": False,
            }
    messages = [
        {"role": "system", "content": preamble()},
        {"role": "user", "content": row["prompt"]},
    ]
    priced = False
    retries = 0
    totals = state.setdefault("_bedrock", {"input": 0, "output": 0, "calls": 0, "missing": False})
    for _ in range(limit):
        if not allow_call():
            return priced, False, None
        certification = {
            "candidateId": state["image_digest"],
            "releaseId": "2026.1",
            "endpointIdentity": endpoint.validated_base_url(),
            "actionId": row["id"],
            "runNonce": uuid.uuid4().hex,
        }
        try:
            content, usage, _elapsed, _proof = client.complete(messages, certification)
        except (CanaryError, ModelFailure, OSError) as exc:
            return priced, False, {
                "kind": "provider_5xx" if priced else "transport_error",
                "replay": type(exc).__name__,
                "detail": _error_detail(exc, state),
                "model_acted": priced,
                "state_changed": any(item.get("tool") for item in transcript),
            }
        priced = True
        messages.append({"role": "assistant", "content": content})
        pair = _usage_pair(usage)
        totals["calls"] += 1
        if pair is None:
            totals["missing"] = True
        else:
            totals["input"] += pair[0]
            totals["output"] += pair[1]
        on_usage(pair)
        if isinstance(usage, dict):
            state["_usage"] = usage
        try:
            kind, action = parse_model_action(content)
        except ModelFailure:
            if retries >= 1:
                transcript.append({"kind": "report", "text": content, "report": {"summary": content}})
                return priced, True, None
            retries += 1
            transcript.append({"kind": "nudge", "reason": "invalid-model-action"})
            messages.append({"role": "user", "content": retry_prompt()})
            continue
        parsed = _tool_action(kind, action)
        if parsed["tool"] == "finish":
            transcript.append({"kind": "report", "text": json.dumps(parsed["arguments"]), "report": parsed["arguments"]})
            return priced, True, None
        try:
            result = apply_action(state, {"tool": parsed["tool"], **parsed["arguments"]})
        except (KeyError, TypeError, ValueError):
            result = {"ok": False, "error": "the arguments do not match this operation"}
        transcript.append({"tool": parsed["tool"], "arguments": parsed["arguments"], "result": result})
        messages.append({"role": "user", "content": json.dumps({"result": result}, default=str)})
    return priced, True, None


def _blind(row: dict, root: Path | None = None) -> None:
    allow = advertised_tools()
    assert_clean(
        tree=root,
        texts={
            "prompt": row["prompt"],
            "preamble": preamble(),
            "retry": retry_prompt(),
        },
        env_names=list(ENV_NAMES),
        names=list(PROCESS_NAMES),
        allow=allow,
    )


def run_row(
    row: dict,
    *,
    arm: str,
    model: str,
    work: Path,
    execute: bool,
    priced_ok: bool,
    allow_call=None,
    on_usage=None,
) -> dict:
    if model != "stub" or priced_ok:
        raise RuntimeError("not-real: only unpriced stub self-tests are supported")
    if allow_call is None:
        allow_call = lambda: True
    if on_usage is None:
        on_usage = lambda pair: None
    state, ground = build_case(row["scenario"], int(row["trial"]), phrasing=row["phrasing"])
    if state["town_name"] != row["town"] or state["town_slug"] != row["town_slug"]:
        raise RuntimeError(f"{row['id']} town does not match the seed")
    root = write_fixture(work / row["id"], state)
    _blind(row, root)
    plan = local_docker_plan(root, state)
    evidence = None
    started = False
    try:
        if execute:
            compose_note = start_deployment(root, state, proxy=(model == "live"))
            started = True
            if compose_note.get("infrastructure"):
                evidence = {
                    "kind": "harness_crash",
                    "replay": f"compose-up:{compose_note['project']}",
                    "model_acted": False,
                    "state_changed": False,
                }
        before = copy.deepcopy(state)
        transcript: list[dict] = []
        priced_call = False
        cap_exhausted = False
        if evidence is None and model == "live":
            if not priced_ok:
                evidence = {"kind": "harness_crash", "replay": "priced-refused", "model_acted": False, "state_changed": False}
            else:
                try:
                    priced_call, completed, live_evidence = live_actions(
                        state, transcript, row, arm, ARMS[arm]["max_actions"],
                        allow_call=allow_call, on_usage=on_usage, wait_for_server=execute,
                    )
                except Exception as exc:
                    acted = any(item.get("tool") for item in transcript)
                    completed = False
                    live_evidence = {
                        "kind": "harness_crash",
                        "replay": type(exc).__name__,
                        "model_acted": priced_call or acted,
                        "state_changed": acted or state != before,
                    }
                if not completed:
                    evidence = live_evidence
        elif evidence is None and model == "stub":
            transcript, cap_exhausted = _drive_stub(row["scenario"], state, ARMS[arm]["max_actions"])
        measured = _measure(row["prompt"], transcript)
        bedrock = state.get("_bedrock") or {}
        if model == "live" and priced_call and bedrock.get("calls") and not bedrock.get("missing"):
            measured = {
                "input_tokens": bedrock["input"],
                "output_tokens": bedrock["output"],
                "context_tokens": bedrock["input"] + bedrock["output"],
            }
            cost = _trial_cost(arm, bedrock["input"], bedrock["output"])
            cost_known = True
            usage_source = "bedrock-input-token-count"
        elif model == "live" and priced_call:
            cost = None
            cost_known = False
            usage_source = "missing"
        else:
            cost = None
            cost_known = False
            usage_source = "stub-char-estimate"
        record = {
            "id": row["id"],
            "scenario": row["scenario"],
            "phrasing": row["phrasing"],
            "trial": int(row["trial"]),
            "target": "local-docker",
            "town": row["town"],
            "outcome": "fail",
            "false_success": False,
            "unsafe": False,
            "invalid": False,
            "cap_hit": False,
            "o05": None,
            "healthy_false_alarm": False,
            "o04_healthy": row["scenario"] == "O04" and not ground["issues"],
            "infrastructure": False,
            "infrastructure_evidence": None,
            "replacement": None,
            "priced_call": priced_call,
            "server_readback": False,
            "prior_image": plan["prior_image"],
            "arm": arm,
            "guidance": GUIDANCE,
            "shipped_temperature": SHIPPED_TEMPERATURE,
            "usage_source": usage_source,
            "cost_usd": cost,
            "cost_known": cost_known,
            **measured,
            "actions": sum(1 for item in transcript if item.get("tool")),
            "nudges": sum(1 for item in transcript if item.get("kind") == "nudge"),
            "failed_actions": sum(1 for item in transcript if item.get("tool") and isinstance(item.get("result"), dict) and item["result"].get("ok") is False),
        }
        if evidence is not None and infrastructure_evidence(evidence):
            record["infrastructure"] = True
            record["infrastructure_evidence"] = {key: evidence[key] for key in ("kind", "replay", "detail") if key in evidence}
            record["outcome"] = "infrastructure"
            _write_artifacts(work.parent, row, transcript, state)
            return record
        from oracles import grade

        graded = grade(
            before,
            state,
            transcript,
            ground,
            actions=record["actions"] + (1 if cap_exhausted else 0),
            tokens=measured["context_tokens"] if measured["context_tokens"] > MAX_CONTEXT_TOKENS else None,
        )
        if cap_exhausted or measured["context_tokens"] > MAX_CONTEXT_TOKENS or record["actions"] > MAX_ACTIONS:
            graded["cap_hit"] = True
            if graded["outcome"] == "success":
                graded["outcome"] = "fail"
        if model == "live" and priced_ok:
            graded = _resolve_ambiguous(graded, row["id"], work.parent, live=True)
        record["outcome"] = graded["outcome"]
        record["false_success"] = graded["false_success"]
        record["unsafe"] = graded["unsafe"]
        record["invalid"] = graded["invalid"]
        record["cap_hit"] = graded["cap_hit"]
        record["o05"] = graded["o05"]
        record["healthy_false_alarm"] = graded["healthy_false_alarm"]
        _write_artifacts(work.parent, row, transcript, state)
        return record
    finally:
        if started:
            stop_deployment(root, state)


def _resolve_ambiguous(graded: dict, row_id: str, out: Path, *, live: bool) -> dict:
    packet = graded.get("judge_packet")
    if not packet:
        return graded
    checks = packet.get("checks") if isinstance(packet, dict) else None
    if not isinstance(checks, list) or not checks or any(check.get("task") == "review" for check in checks):
        _write_review(out, row_id, packet, "no-rubric" if live else HUMAN_REVIEW)
        return graded
    if not live or JUDGE["mode"] == HUMAN_REVIEW:
        if JUDGE["mode"] == HUMAN_REVIEW:
            _write_review(out, row_id, packet, HUMAN_REVIEW)
        return graded
    from judge import apply_fallback, classify_decision, invoke_fallback

    decisions = {}
    for check in checks:
        one = dict(check)
        one["summary"] = packet.get("summary")
        try:
            decision = classify_decision(invoke_fallback(one))
            detail = "unresolved"
        except Exception:
            decision = None
            detail = HUMAN_REVIEW
        if decision is None:
            _write_review(out, row_id, packet, detail)
            return graded
        decisions[check.get("task")] = decision
    return apply_fallback(graded, decisions)


def _error_detail(exc: Exception, state: dict) -> str:
    """Keep bounded, redacted transport diagnostics in trial artifacts."""
    redactor = _redactor(state)
    from client import Redactor
    sensitive = [os.environ.get(name, "") for name in (
        "TERMINAL_MODEL_API_KEY", "HONUA_ADMIN_PASSWORD", "HONUA_CANARY_AWS_ACCESS_KEY_ID",
        "HONUA_CANARY_AWS_SECRET_ACCESS_KEY", "HONUA_CANARY_AWS_SESSION_TOKEN",
        "HONUA_CANARY_TRANSCRIPT_SIGNING_SEED",
    )]
    return Redactor(sensitive).text(redactor.text(str(exc))).replace("\n", " ")[:500]


def _redactor(state: dict):
    from client import Redactor

    return Redactor(sensitive_values=[
        NOTES_SECRET,
        INITIAL_PASSWORD,
        ROTATED_PASSWORD,
        state.get("notes_secret") or "",
        state.get("database_password") or "",
    ])


def _write_review(out: Path, row_id: str, packet: dict, detail: str) -> None:
    path = out / "review-queue" / f"{row_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": row_id,
        "detail": detail,
        "packet": packet,
    }, sort_keys=True) + "\n", encoding="utf-8")


def _write_artifacts(out: Path, row: dict, transcript: list[dict], state: dict) -> None:
    from client import write_receipt

    redactor = _redactor(state)
    _write_receipt_body(out / "receipts" / f"{row['id']}.json", row, redactor.value(transcript), write_receipt)
    for folder, payload in (
        ("transcripts", redactor.value(transcript)),
        ("audit", redactor.value(state.get("audit") or [])),
    ):
        path = out / folder / f"{row['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_receipt_body(path: Path, row: dict, transcript, write_receipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_receipt(path, {
        "id": row["id"],
        "phrasing": row["phrasing"],
        "guidance": GUIDANCE,
        "tools": transcript,
        "server_readback": False,
    })


def _emit(summary: dict) -> int:
    reasons = (summary.get("stop") or {}).get("reasons") or []
    sys.stdout.write(summary_line(summary) + "\n")
    return 0 if not reasons else 2


def _select_rows(rows: list[dict], *, pilot: bool, max_rows: int | None) -> list[dict]:
    if pilot:
        by_id = {row["id"]: row for row in rows}
        return [by_id[row_id] for row_id in PILOT_IDS if row_id in by_id]
    if max_rows is not None:
        return rows[:max_rows]
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate-stage runner. Prints one JSON summary line.")
    parser.add_argument("--target", choices=("local-docker", "aws-ecs"), default="local-docker")
    parser.add_argument("--model", choices=("stub", "live"), default="stub")
    parser.add_argument("--arm", choices=tuple(ARMS), default="primary")
    parser.add_argument("--rows", type=Path, default=DEFAULT_ROWS)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--qualification", action="store_true")
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--execute-compose", action="store_true")
    parser.add_argument("--image-source", choices=("local", "pull"), default="local")
    parser.add_argument("--newer-image", default=None)
    parser.add_argument("--newer-sha", default=None)
    parser.add_argument("--newer-digest", default=None)
    parser.add_argument("--server-source", type=Path)
    parser.add_argument("--prior-image", default=None)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--spend-cap", default=str(SPEND_CAP_USD))
    parser.add_argument("--pilot-summary", type=Path)
    parser.add_argument("--priced-ok", action="store_true")
    parser.add_argument("--write-rows", nargs="?", const=str(DEFAULT_ROWS), default=None)
    args = parser.parse_args(argv)

    if args.model == "stub" and args.priced_ok:
        sys.stderr.write("not-real: stub mode cannot be used with --priced-ok\n")
        return 2

    if args.write_rows:
        from rows import write_development

        written = write_development(Path(args.write_rows))
        sys.stderr.write(f"wrote {written}\n")
        return 0

    if args.self_test:
        report = preflight()
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0 if report["ok"] else 2

    if args.pilot and args.max_rows not in (None, 3):
        sys.stderr.write("the pilot is the three local trials named by the preregistration\n")
        return 2

    if args.prior_image and args.prior_image != PRIOR_IMAGE:
        sys.stderr.write("the prior image is the published release named by the preregistration\n")
        return 2
    if args.newer_image and args.newer_image != NEWER_IMAGE:
        sys.stderr.write("the newer image is the trunk build named by the preregistration\n")
        return 2

    blocked = _pilot_block(args.pilot_summary, pilot=bool(args.pilot), arm=args.arm)
    if blocked:
        summary = summarize([])
        summary.update({
            "target": args.target, "arm": args.arm, "model": None, "priced": False,
            "driver": args.model, "status": "stopped",
        })
        summary["stop"]["reasons"] = ["pilot_required"]
        return _finish(args, summary, blocked)

    try:
        cap = Decimal(str(args.spend_cap))
    except Exception:
        sys.stderr.write("spend cap is not a USD amount\n")
        return 2
    if cap < 0:
        sys.stderr.write("spend cap is not a USD amount\n")
        return 2

    gate = preflight()
    ledger = _load_ledger(args.ledger, cap)
    ledger_state = _ledger_status(ledger)

    if args.target == "aws-ecs":
        plan = aws_ecs_plan()
        summary = summarize([])
        summary["target"] = "aws-ecs"
        summary["arm"] = args.arm
        summary["model"] = None
        summary["priced"] = False
        summary["driver"] = "unwired"
        summary["status"] = plan["status"]
        summary["stop"]["reasons"] = ["unwired", *stop_reasons(
            summary, pilot=bool(args.pilot), preflight_ok=gate["ok"], ledger_status=ledger_state,
        )]
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "target.json").write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")
        return _finish(args, summary)

    rows = _read_rows(args.rows)
    if any(is_held_out_row(row) for row in rows) and not args.qualification:
        sys.stderr.write("held-out rows require --qualification\n")
        return 2
    load_prompts(qualification=bool(args.qualification and any(is_held_out_row(row) for row in rows)))
    rows = _select_rows(rows, pilot=bool(args.pilot), max_rows=args.max_rows)
    if not rows:
        sys.stderr.write("no rows\n")
        return 2
    if args.out is None:
        sys.stderr.write("--out is required\n")
        return 2

    for row in rows:
        _blind(row)

    if not gate["ok"] or _exhausted(ledger):
        summary = summarize([])
        summary.update({
            "target": args.target,
            "arm": args.arm,
            "model": None,
            "priced": False,
            "driver": args.model,
            "status": "stopped",
        })
        summary["stop"]["reasons"] = stop_reasons(
            summary, pilot=bool(args.pilot), preflight_ok=gate["ok"],
            ledger_status="budget_exhausted" if _exhausted(ledger) else ledger_state,
        )
        return _finish(args, summary)

    if args.model == "live" and not args.priced_ok:
        summary = summarize([])
        summary.update({
            "target": args.target,
            "arm": args.arm,
            "model": None,
            "priced": False,
            "driver": "live",
            "status": "priced-refused",
        })
        summary["stop"]["reasons"] = ["priced_refused"]
        sys.stderr.write("live mode needs --priced-ok and was not started\n")
        return _finish(args, summary)

    if args.model == "live":
        os.environ["HONUA_CANARY_BEDROCK_MODEL"] = ARMS[args.arm]["bedrock"]
        os.environ.setdefault("HONUA_CANARY_BEDROCK_REGION", "us-east-1")
        blocked = _endpoint_block(args.arm)
        if blocked:
            summary = summarize([])
            summary.update({
                "target": args.target,
                "arm": args.arm,
                "model": None,
                "priced": False,
                "driver": "live",
                "status": "stopped",
            })
            summary["stop"]["reasons"] = ["endpoint"]
            return _finish(args, summary, blocked)

    if args.model == "live":
        summary = summarize([])
        summary.update({"target": args.target, "arm": args.arm, "model": None,
                        "priced": False, "driver": "live", "status": "not-real"})
        summary["stop"]["reasons"] = ["not-real"]
        return _finish(args, summary, "not-real: no scenarios have real operations, probes, and independent readback")

    images = None
    if args.execute_compose:
        prepared = prepare_images(
            source=args.image_source,
            newer_sha=args.newer_sha,
            newer_digest=args.newer_digest,
            server_source=args.server_source,
        )
        images = {"prior": prepared["prior"], "newer": prepared["newer"]}
        if not prepared["ok"]:
            summary = summarize([])
            summary.update({
                "target": args.target, "arm": args.arm, "model": None, "priced": False,
                "driver": args.model, "status": "stopped", "images": images,
            })
            summary["stop"]["reasons"] = ["images"]
            return _finish(args, summary, prepared.get("detail") or "images are not available")

    if args.qualification and any(is_held_out_row(row) for row in rows):
        from scenarios import exposure_record

        (args.out / "held-out-exposure.json").write_text(
            json.dumps({"opened": True, "prior": exposure_record()}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    work = args.out / "fixtures"
    work.mkdir(parents=True, exist_ok=True)
    (args.out / "transcripts").mkdir(parents=True, exist_ok=True)
    (args.out / "audit").mkdir(parents=True, exist_ok=True)
    if ledger is not None and args.ledger is not None:
        _write_ledger(args.ledger, ledger)
    trials = []
    trials_path = args.out / "trials.jsonl"
    handle = None
    try:
        for row in rows:
            if _exhausted(ledger):
                break
            running = {"spent": Decimal(str(ledger["spent_usd"]))} if ledger is not None else {"spent": Decimal(0)}

            def allow_call(running=running):
                if ledger is None:
                    return True
                return running["spent"] < Decimal(str(ledger["cap_usd"]))

            def on_usage(pair, running=running):
                if ledger is None or pair is None:
                    return
                running["spent"] += Decimal(_trial_cost(args.arm, pair[0], pair[1]))

            if handle is None:
                handle = trials_path.open("w", encoding="utf-8")
            record = run_row(
                row,
                arm=args.arm,
                model=args.model,
                work=work,
                execute=args.execute_compose,
                priced_ok=args.priced_ok,
                allow_call=allow_call,
                on_usage=on_usage,
            )
            trials.append(record)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            if ledger is not None:
                if record.get("cost_known") and record.get("cost_usd") is not None:
                    spent = Decimal(str(ledger["spent_usd"])) + Decimal(record["cost_usd"])
                    ledger["spent_usd"] = _money(spent)
                    if spent >= Decimal(str(ledger["cap_usd"])):
                        ledger["status"] = "budget_exhausted"
                elif record.get("priced_call"):
                    ledger["status"] = "usage_missing"
                ledger["trials"].append({
                    "id": record["id"],
                    "input_tokens": record.get("input_tokens"),
                    "output_tokens": record.get("output_tokens"),
                    "cost_usd": record.get("cost_usd"),
                    "usage_source": record.get("usage_source"),
                })
                _write_ledger(args.ledger, ledger)
    finally:
        if handle is not None:
            handle.close()

    summary = summarize(trials)
    summary.update({
        "target": args.target,
        "arm": args.arm,
        "model": ARMS[args.arm]["model"] if any(trial["priced_call"] for trial in trials) else None,
        "priced": any(trial["priced_call"] for trial in trials),
        "driver": "stub" if args.model == "stub" else "live",
        "status": "complete",
    })
    if images is not None:
        summary["images"] = images
    reasons = stop_reasons(
        summary, pilot=bool(args.pilot), preflight_ok=True,
        ledger_status=_ledger_status(ledger), trials=trials,
    )
    if ledger is not None and ledger.get("status") == "usage_missing":
        reasons.append("usage")
    summary["stop"]["reasons"] = reasons
    if ledger is not None and _exhausted(ledger) and "budget_exhausted" not in reasons and ledger.get("status") == "budget_exhausted":
        reasons.append("budget_exhausted")
    return _finish(args, summary)


def _finish(args, summary: dict, message: str | None = None) -> int:
    summary["scope"] = "pilot" if args.pilot else "full"
    if message:
        sys.stderr.write(message + "\n")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(summary_line(summary) + "\n", encoding="utf-8")
    return _emit(summary)


if __name__ == "__main__":
    raise SystemExit(main())
