"""Run seeded operate scenarios and print one v2 summary line.

The scripted operator is the local check. A live model call needs --model live and --priced-ok,
and this lane does not pass those flags.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path

from blinding import assert_clean
from catalog import advertised_tools
from constants import (
    ARMS,
    GUIDANCE,
    MAX_ACTIONS,
    MAX_CONTEXT_TOKENS,
    NEWER_IMAGE,
    NOTES_SECRET,
    PILOT_IDS,
    PRIOR_IMAGE,
    ROTATED_PASSWORD,
    SHIPPED_TEMPERATURE,
)
from controller import apply_action
from fixture import ENV_NAMES, PROCESS_NAMES, write_fixture
from oracles import infrastructure_evidence
from scenarios import is_held_out_row, load_prompts, preamble, retry_prompt
from score import preflight, stop_reasons, summarize, summary_line
from state import INITIAL_PASSWORD, build_case
from stub import stub_next
from targets import aws_ecs_plan, execute_local_docker, local_docker_plan

HERE = Path(__file__).resolve().parent
DEFAULT_ROWS = HERE / "rows" / "development.jsonl"


def _read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _ledger_status(path: Path | None) -> str | None:
    if path is None:
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    status = document.get("status")
    return status if isinstance(status, str) else None


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


def live_actions(state: dict, transcript: list[dict], row: dict, arm: str, limit: int) -> tuple[bool, bool, dict | None]:
    """Call the canary client. Returns (priced, completed, evidence)."""
    from client import CanaryError, ModelFailure, OperateClient, build_endpoint, parse_model_action

    endpoint = build_endpoint(arm)
    if not endpoint.configured:
        return False, False, {
            "kind": "transport_error",
            "replay": "endpoint-unconfigured",
            "model_acted": False,
            "state_changed": False,
        }
    client = OperateClient(endpoint)
    messages = [
        {"role": "system", "content": preamble()},
        {"role": "user", "content": row["prompt"]},
    ]
    priced = False
    retries = 0
    for _ in range(limit):
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
                "model_acted": priced,
                "state_changed": any(item.get("tool") for item in transcript),
            }
        priced = True
        messages.append({"role": "assistant", "content": content})
        if isinstance(usage, dict):
            state["_usage"] = usage
        try:
            kind, action = parse_model_action(content)
        except ModelFailure:
            if retries >= 1:
                transcript.append({"kind": "report", "text": content, "report": {"summary": content}})
                return priced, True, None
            retries += 1
            messages.append({"role": "user", "content": retry_prompt()})
            continue
        parsed = _tool_action(kind, action)
        if parsed["tool"] == "finish":
            transcript.append({"kind": "report", "text": json.dumps(parsed["arguments"]), "report": parsed["arguments"]})
            return priced, True, None
        result = apply_action(state, {"tool": parsed["tool"], **parsed["arguments"]})
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


def run_row(row: dict, *, arm: str, model: str, work: Path, execute: bool, priced_ok: bool) -> dict:
    state, ground = build_case(row["scenario"], int(row["trial"]), phrasing=row["phrasing"])
    if state["town_name"] != row["town"] or state["town_slug"] != row["town_slug"]:
        raise RuntimeError(f"{row['id']} town does not match the seed")
    root = write_fixture(work / row["id"], state)
    _blind(row, root)
    plan = local_docker_plan(root, state)
    compose_note = {"executed": False, "server_readback": False}
    evidence = None
    if execute:
        compose_note = execute_local_docker(root, state)
        if compose_note.get("infrastructure"):
            evidence = {
                "kind": "harness_crash",
                "replay": "compose-config",
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
                )
            except Exception as exc:
                priced_call, completed, live_evidence = False, False, {
                    "kind": "harness_crash",
                    "replay": type(exc).__name__,
                    "model_acted": False,
                    "state_changed": False,
                }
            if not completed:
                evidence = live_evidence
    elif evidence is None and model == "stub":
        transcript, cap_exhausted = _drive_stub(row["scenario"], state, ARMS[arm]["max_actions"])
    measured = _measure(row["prompt"], transcript)
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
        **measured,
        "actions": sum(1 for item in transcript if item.get("tool")),
    }
    if evidence is not None and infrastructure_evidence(evidence):
        record["infrastructure"] = True
        record["infrastructure_evidence"] = {"kind": evidence["kind"], "replay": evidence["replay"]}
        record["outcome"] = "infrastructure"
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
    record["outcome"] = graded["outcome"]
    record["false_success"] = graded["false_success"]
    record["unsafe"] = graded["unsafe"]
    record["invalid"] = graded["invalid"]
    record["cap_hit"] = graded["cap_hit"]
    record["o05"] = graded["o05"]
    record["healthy_false_alarm"] = graded["healthy_false_alarm"]
    _write_receipt(work.parent / "receipts" / f"{row['id']}.json", row, transcript, state)
    return record


def _write_receipt(path: Path, row: dict, transcript: list[dict], state: dict) -> None:
    from client import Redactor, write_receipt

    redactor = Redactor(sensitive_values=[
        NOTES_SECRET,
        INITIAL_PASSWORD,
        ROTATED_PASSWORD,
        state.get("notes_secret") or "",
        state.get("database_password") or "",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_receipt(path, {
        "id": row["id"],
        "phrasing": row["phrasing"],
        "guidance": GUIDANCE,
        "tools": redactor.value(transcript),
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
    parser.add_argument("--newer-image", default=None)
    parser.add_argument("--prior-image", default=None)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--priced-ok", action="store_true")
    parser.add_argument("--write-rows", nargs="?", const=str(DEFAULT_ROWS), default=None)
    args = parser.parse_args(argv)

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

    gate = preflight()
    ledger = _ledger_status(args.ledger)

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
            summary, pilot=False, preflight_ok=gate["ok"], ledger_status=ledger,
        )]
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "summary.json").write_text(summary_line(summary) + "\n", encoding="utf-8")
            (args.out / "target.json").write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")
        return _emit(summary)

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

    if not gate["ok"] or ledger == "budget_exhausted":
        summary = summarize([])
        summary.update({
            "target": args.target,
            "arm": args.arm,
            "model": None,
            "priced": False,
            "driver": args.model,
            "status": "stopped",
        })
        summary["stop"]["reasons"] = stop_reasons(summary, pilot=bool(args.pilot), preflight_ok=gate["ok"], ledger_status=ledger)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(summary_line(summary) + "\n", encoding="utf-8")
        return _emit(summary)

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
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(summary_line(summary) + "\n", encoding="utf-8")
        sys.stderr.write("live mode needs --priced-ok and was not started\n")
        return _emit(summary)

    if args.qualification and any(is_held_out_row(row) for row in rows):
        from scenarios import exposure_record

        (args.out / "held-out-exposure.json").write_text(
            json.dumps({"opened": True, "prior": exposure_record()}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    work = args.out / "fixtures"
    work.mkdir(parents=True, exist_ok=True)
    trials = []
    trials_path = args.out / "trials.jsonl"
    with trials_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            record = run_row(
                row,
                arm=args.arm,
                model=args.model,
                work=work,
                execute=args.execute_compose,
                priced_ok=args.priced_ok,
            )
            trials.append(record)
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    summary = summarize(trials)
    summary.update({
        "target": args.target,
        "arm": args.arm,
        "model": ARMS[args.arm]["model"] if any(trial["priced_call"] for trial in trials) else None,
        "priced": any(trial["priced_call"] for trial in trials),
        "driver": "stub" if args.model == "stub" else "live",
        "status": "complete",
    })
    summary["stop"]["reasons"] = stop_reasons(
        summary, pilot=bool(args.pilot), preflight_ok=True, ledger_status=ledger, trials=trials,
    )
    line = summary_line(summary)
    (args.out / "summary.json").write_text(line + "\n", encoding="utf-8")
    return _emit(summary)


if __name__ == "__main__":
    raise SystemExit(main())
