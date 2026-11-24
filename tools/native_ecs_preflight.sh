#!/usr/bin/env bash
set -euo pipefail

qualification=()
if [[ -n "${CANDIDATE_REF:-}" ]]; then
  python - <<'PY'
import yaml

with open("platform-manifest.yaml", encoding="utf-8") as stream:
    manifest = yaml.safe_load(stream)
if manifest["protocolCertification"]["ledger"]["status"] != "bound":
    raise SystemExit("Nightly qualification requires the bound protocol ledger")
PY
  qualification+=(--qualification)
  python tools/generate_platform_lock.py --manifest platform-manifest.yaml \
    --matrix compatibility-matrix.yaml --output platform-lock.json "${qualification[@]}"
fi

python tools/platform_lock_bundle.py platform-lock.json --manifest platform-manifest.yaml \
  --matrix compatibility-matrix.yaml --label "$PLATFORM_LABEL" \
  --out-dir frozen-lock "${qualification[@]}"
cmp platform-lock.json frozen-lock/platform-lock.json

mkdir -p native-ecs-diagnostics
python - <<'PY' > native-ecs-diagnostics/candidate.json
import hashlib
import json
import os
from pathlib import Path
import re

import yaml

from tools.image_platforms import image_platform_digests

manifest = Path("platform-manifest.yaml").read_bytes()
candidate_digest = hashlib.sha256(manifest).hexdigest()
lock_digest = "sha256:" + hashlib.sha256(Path("frozen-lock/platform-lock.json").read_bytes()).hexdigest()
server = yaml.safe_load(manifest)["components"]["honua-server"]
image = server["image"].split("@", 1)[0] + "@" + server["digest"]
if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
    raise SystemExit("Frozen server image must be digest pinned")
platform_digests = image_platform_digests(
    server.get("platformDigests"), index_digest=server["digest"], architectures=server.get("architectures")
)
identity = hashlib.sha256(f"{candidate_digest}\n{lock_digest}\n".encode()).hexdigest()
print(json.dumps({
    "candidateRef": os.environ.get("CANDIDATE_REF", ""),
    "candidateDigest": candidate_digest,
    "lockDigest": lock_digest,
    "qualificationIdentity": identity,
    "serverImage": image,
    "serverSha": server["sha"],
    "imageDigest": server["digest"],
    "platformDigests": platform_digests,
}, indent=2))
PY
