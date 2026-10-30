"""Run seeded operate scenarios and print one bar summary line.

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
from constants import (
    ARMS,
    NEWER_IMAGE,
    NOTES_SECRET,
    PHRASING_HELD_OUT,
    PRIOR_IMAGE,
    ROTATED_PASSWORD,
)
from controller import apply_action
from fixture import ENV_NAMES, write_fixture
from scenarios import load_prompts, preamble, retry_prompt
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


def _arguments(action: dict) -> dict:
    if "arguments" in action and isinstance(action["arguments"], dict):
        return dict(action["arguments"])
    return {key: value for key, value in action.items() if key != "tool"}


def _drive_stub(scenario: str, state: dict, limit: int) -> list[dict]:
    transcript: list[dict] = []
    for _ in range(limit):
        action = stub_next(scenario, state, transcript)
        if action["tool"] == "finish":
            transcript.append(action)
            return transcript
        result = apply_action(state, action)
        transcript.append({"tool": action["tool"], "arguments": _arguments(action), "result": result})
    return transcript


def _tool_action(kind: str, action: dict) -> dict:
    if kind == "tool_call":
        return {"tool": action["tool"], "arguments": dict(action.get("arguments") or {})}
    return {"tool": "terminal_command", "arguments": {"command": action.get("command", "")}}


def live_actions(state: dict, transcript: list[dict], row: dict, arm: str, limit: int) -> tuple[bool, bool]:
    """Call the canary client. Returns (priced call issued, call completed without an infra error)."""
    from client import CanaryError, ModelFailure, OperateClient, arm_temperature, build_endpoint, parse_model_action

    endpoint = build_endpoint(arm)
    if not endpoint.configured:
        return False, False
    client = OperateClient(endpoint, temperature=arm_temperature(arm))
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
            content, _usage, _elapsed, _proof = client.complete(messages, certification)
        except (CanaryError, ModelFailure, OSError):
            return priced, False
        priced = True
        messages.append({"role": "assistant", "content": content})
        try:
            kind, action = parse_model_action(content)
        except ModelFailure:
            if retries >= 1:
                return priced, True
            retries += 1
            messages.append({"role": "user", "content": retry_prompt()})
            continue
        parsed = _tool_action(kind, action)
        if parsed["tool"] == "finish":
            transcript.append({"tool": "finish", "arguments": parsed["arguments"]})
            return priced, True
        result = apply_action(state, {"tool": parsed["tool"], **parsed["arguments"]})
        transcript.append({"tool": parsed["tool"], "arguments": parsed["arguments"], "result": result})
        messages.append({"role": "user", "content": json.dumps({"result": result}, default=str)})
    return priced, True


def run_row(row: dict, *, arm: str, model: str, work: Path, execute: bool, priced_ok: bool) -> dict:
    state, ground = build_case(row["scenario"], int(row["trial"]))
    if state["town_name"] != row["town"] or state["town_slug"] != row["town_slug"]:
        raise RuntimeError(f"{row['id']} town does not match the seed")
    root = write_fixture(work / row["id"], state)
    assert_clean(
        tree=root,
        texts={"prompt": row["prompt"], "preamble": preamble(), "retry": retry_prompt()},
        env_names=list(ENV_NAMES),
    )
    plan = local_docker_plan(root, state)
    compose_note = {"executed": False, "server_readback": False}
    if execute:
        compose_note = execute_local_docker(root, state)
    before = copy.deepcopy(state)
    transcript: list[dict] = []
    infrastructure = bool(compose_note.get("infrastructure"))
    priced_call = False
    if not infrastructure and model == "live":
        if not priced_ok:
            infrastructure = True
        else:
            try:
                priced_call, completed = live_actions(state, transcript, row, arm, ARMS[arm]["max_actions"])
            except Exception:
                priced_call, completed = False, False
            if not completed:
                infrastructure = True
    elif not infrastructure and model == "stub":
        transcript = _drive_stub(row["scenario"], state, ARMS[arm]["max_actions"])
    record = {
        "id": row["id"],
        "scenario": row["scenario"],
        "phrasing": row["phrasing"],
        "trial": int(row["trial"]),
        "target": "local-docker",
        "town": row["town"],
        "passed": False,
        "destructive": False,
        "false_success": False,
        "healthy_false_alarm": False,
        "infrastructure": infrastructure,
        "o04_healthy": row["scenario"] == "O04" and not ground["issues"],
        "priced_call": priced_call,
        "server_readback": False,
        "prior_image": plan["prior_image"],
        "arm_label": arm,
    }
    if infrastructure:
        return record
    from oracles import grade

    graded = grade(before, state, transcript, ground, arm_label=arm)
    record["passed"] = graded["passed"]
    record["destructive"] = graded["destructive"]
    record["false_success"] = graded["false_success"]
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
    write_receipt(path, {
        "id": row["id"],
        "arm_label": row.get("arm_label") or "",
        "phrasing": row["phrasing"],
        "tools": redactor.value(transcript),
        "server_readback": False,
    })


def _empty_summary(target: str, arm: str, driver: str, status: str) -> dict:
    summary = summarize([])
    summary.update({
        "target": target,
        "arm_label": arm,
        "model": None,
        "priced": False,
        "driver": driver,
        "status": status,
    })
    return summary


def _emit(summary: dict) -> int:
    reasons = summary.get("stop_reasons") or []
    sys.stdout.write(summary_line(summary) + "\n")
    return 0 if summary["bars_ok"] and not reasons else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate-stage runner. Prints one JSON summary line.")
    parser.add_argument("--target", choices=("local-docker", "aws-ecs"), default="local-docker")
    parser.add_argument("--model", choices=("stub", "live"), default="stub")
    parser.add_argument("--arm", choices=tuple(ARMS), default="A")
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
        sys.stderr.write("--pilot runs the first 3 rows\n")
        return 2
    if args.pilot:
        args.max_rows = 3
    pilot = bool(args.pilot or args.max_rows == 3)

    if args.prior_image and args.prior_image != PRIOR_IMAGE:
        sys.stderr.write("the prior image is the published release named by the preregistration\n")
        return 2
    if args.newer_image and args.newer_image != NEWER_IMAGE:
        sys.stderr.write("the newer image is the trunk build named by the preregistration\n")
        return 2

    if args.target == "aws-ecs":
        plan = aws_ecs_plan()
        summary = _empty_summary("aws-ecs", args.arm, "unwired", plan["status"])
        gate = preflight()
        summary["stop_reasons"] = stop_reasons(
            summary, pilot=False, preflight_ok=gate["ok"], ledger_status=_ledger_status(args.ledger),
        )
        if "unwired" not in summary["stop_reasons"]:
            summary["stop_reasons"] = ["unwired", *summary["stop_reasons"]]
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "summary.json").write_text(summary_line(summary) + "\n", encoding="utf-8")
            (args.out / "target.json").write_text(json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8")
        return _emit(summary)

    rows = _read_rows(args.rows)
    if any(row.get("phrasing") == PHRASING_HELD_OUT for row in rows) and not args.qualification:
        sys.stderr.write("phrasing C requires --qualification\n")
        return 2
    needs_held_out = args.qualification and any(row.get("phrasing") == PHRASING_HELD_OUT for row in rows)
    load_prompts(qualification=needs_held_out)
    if args.max_rows is not None:
        rows = rows[: args.max_rows]
    if not rows:
        sys.stderr.write("no rows\n")
        return 2
    if args.out is None:
        sys.stderr.write("--out is required\n")
        return 2

    for row in rows:
        assert_clean(texts={row["id"]: row["prompt"], "preamble": preamble(), "retry": retry_prompt()})

    gate = preflight()
    ledger = _ledger_status(args.ledger)
    if not gate["ok"] or ledger == "budget_exhausted":
        summary = _empty_summary(args.target, args.arm, args.model, "stopped")
        summary["pilot_passes"] = 0 if pilot else None
        summary["pilot_destructive"] = False
        summary["stop_reasons"] = stop_reasons(summary, pilot=pilot, preflight_ok=gate["ok"], ledger_status=ledger)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(summary_line(summary) + "\n", encoding="utf-8")
        return _emit(summary)

    if args.model == "live" and not args.priced_ok:
        summary = _empty_summary(args.target, args.arm, "live", "priced-refused")
        summary["stop_reasons"] = ["priced_refused"]
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "summary.json").write_text(summary_line(summary) + "\n", encoding="utf-8")
        sys.stderr.write("live mode needs --priced-ok and was not started\n")
        return _emit(summary)

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
    counted = [trial for trial in trials if not trial["infrastructure"]]
    summary.update({
        "target": args.target,
        "arm_label": args.arm,
        "model": ARMS[args.arm]["model"] if any(trial["priced_call"] for trial in trials) else None,
        "priced": any(trial["priced_call"] for trial in trials),
        "driver": "stub" if args.model == "stub" else "live",
        "status": "complete",
        "pilot_passes": sum(1 for trial in counted if trial["passed"]) if pilot else None,
        "pilot_destructive": any(trial["destructive"] for trial in counted) if pilot else False,
    })
    summary["stop_reasons"] = stop_reasons(
        summary, pilot=pilot, preflight_ok=True, ledger_status=ledger,
    )
    line = summary_line(summary)
    (args.out / "summary.json").write_text(line + "\n", encoding="utf-8")
    return _emit(summary)


if __name__ == "__main__":
    raise SystemExit(main())
