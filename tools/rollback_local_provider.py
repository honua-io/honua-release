#!/usr/bin/env python3
"""File-backed provider adapter used by deterministic rollback certification."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--fail-provider", default="")
    parser.add_argument("--fail-probe", default="")
    sub = parser.add_subparsers(dest="action", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--provider-id", required=True)
    mutate = sub.add_parser("mutate")
    mutate.add_argument("--provider-id", required=True)
    mutate.add_argument("--expected-json", required=True)
    mutate.add_argument("--mutation-id", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--kind", required=True)
    probe.add_argument("--expected-json", required=True)
    args = parser.parse_args(argv)
    state = load(args.state)
    if args.action == "observe":
        plane = state["planes"].get(args.provider_id)
        if plane is None:
            print(f"unknown provider id: {args.provider_id}")
            return 2
        print(json.dumps({"providerId": args.provider_id, "observed": plane["value"]}, sort_keys=True))
        return 0
    if args.action == "mutate":
        if args.provider_id == args.fail_provider:
            print(f"injected provider failure: {args.provider_id}")
            return 1
        expected = json.loads(args.expected_json)
        previous = state.setdefault("mutations", {}).get(args.mutation_id)
        request = {"providerId": args.provider_id, "expected": expected}
        if previous is not None and previous != request:
            print("mutation id was reused with different bytes")
            return 2
        state["mutations"][args.mutation_id] = request
        state["planes"][args.provider_id]["value"] = expected
        write(args.state, state)
        print(json.dumps({"accepted": True, "mutationId": args.mutation_id, "observed": expected}, sort_keys=True))
        return 0
    expected = json.loads(args.expected_json)
    observed = {provider_id: state["planes"].get(provider_id, {}).get("value") for provider_id in expected}
    ok = observed == expected and args.kind != args.fail_probe
    print(json.dumps({"kind": args.kind, "ok": ok, "expected": expected, "observed": observed}, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
