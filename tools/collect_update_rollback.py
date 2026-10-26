#!/usr/bin/env python3
"""Finalize actual cell cleanup/cost and collect the GA-cell (AWS ECS) transition proof."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from runner.update_rollback import GA_CELLS, ProofError, aggregate, finalize_cell, load_release, require


def collect(reports, prior, candidate, mode, run_id, run_attempt):
    paths = sorted(reports.rglob("update-rollback-cell.json"))
    require(len(paths) == len(GA_CELLS), "exactly one independently retained receipt per GA cell required")
    cells = [json.loads(path.read_text()) for path in paths]
    for cell in cells:
        require(cell.get("prior") == prior.public() and cell.get("candidate") == candidate.public(),
                "cell input binding differs from the authenticated retained builds")
        require(cell.get("runId") == run_id and cell.get("runAttempt") == run_attempt and cell.get("mode") == mode,
                "cell receipt belongs to another workflow attempt or proof mode")
        require(cell.get("fromDigest") == prior.digest_for(cell.get("cell", ""))
                and cell.get("toDigest") == candidate.digest_for(cell.get("cell", "")), "cell image identity differs")
    receipt = aggregate(cells)
    # Rehearsals use the shared execution floor without inventing a lock identity or evidence class.
    import check_promotion_readiness as readiness
    require(readiness._update_rollback_cells(cells, {c: candidate.digest_for(c) for c in readiness.GA_CELLS}),
            "cell proof violates the strict promotion execution/refusal contract")
    if mode == "qualification":
        import jsonschema
        schema = json.loads((Path(__file__).resolve().parents[1] / "certification/promotion-evidence.v1.schema.json").read_text())
        jsonschema.validate(receipt, {"$ref": "#/$defs/updateRollbackReceipt", "$defs": schema["$defs"]})
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", type=Path, help="finalize one raw cell receipt after actual teardown")
    parser.add_argument("--cloud-report", type=Path)
    parser.add_argument("--reports", type=Path)
    parser.add_argument("--prior-inputs", type=Path)
    parser.add_argument("--transition-inputs", type=Path)
    parser.add_argument("--mode", choices=("rehearsal", "qualification"), default="rehearsal")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cell:
            require(args.cloud_report is not None, "actual cloud teardown report required")
            result = finalize_cell(json.loads(args.cell.read_text()), json.loads(args.cloud_report.read_text()))
        else:
            require(args.reports and args.prior_inputs and args.transition_inputs, "retained reports and authenticated inputs required")
            prior, candidate = load_release(args.prior_inputs, args.mode), load_release(args.transition_inputs, args.mode)
            result = collect(args.reports, prior, candidate, args.mode, os.environ.get("GITHUB_RUN_ID", ""),
                             os.environ.get("GITHUB_RUN_ATTEMPT", ""))
    except Exception as error:
        # Input/API/provider diagnostics may include secret paths/coordinates. Keep safe labels only.
        result = {"schema": "honua.aws-update-rollback/v1", "status": "fail", "mode": args.mode,
                  "finding": str(error) if isinstance(error, ProofError) else type(error).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return int(result.get("status") != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
