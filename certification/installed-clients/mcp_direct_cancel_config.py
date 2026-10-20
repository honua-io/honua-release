"""Read-only exact tenant setup for one owned direct-cancel job."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_deployment_identity_configuration import declaration, observe_configuration
from mcp_render_validation import inspect_candidate


def observe(nonce, source, image, *, inspect=inspect_candidate, configuration=observe_configuration):
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{24}", nonce):
        raise ValueError("direct cancel tenant nonce malformed")
    tenant_id = "mcp-deployment-" + nonce[:12]
    declared = declaration(tenant_id)
    observed = configuration(declared, lambda: inspect(source, image))
    return {"schema": "honua.mcp-direct-cancel-tenant/v1", "tenantId": tenant_id,
            "declaration": declared, "observation": observed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(observe(args.nonce, args.candidate_source_sha, args.candidate_image),
                         allow_nan=False, sort_keys=True))
    except (TypeError, KeyError, ValueError, OSError):
        print("owned direct cancel tenant configuration refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
