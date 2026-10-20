#!/usr/bin/env bash
# Shared helpers for the Honua governed Terraform execution substrate.
#
# Sourced by:
#   scripts/terraform-backend-identity.sh
#   scripts/terraform-exact-plan.sh
#   scripts/terraform-exact-apply.sh
#
# Everything in here is evidence-safe by construction: the helpers emit
# identifiers, digests, and configuration facts only. Terraform state contents,
# credentials, tokens, and variable values never reach stdout, a file, or a
# receipt -- values are hashed before they are recorded.
#
# Offline/test mode (HONUA_IAC_OFFLINE=1) replaces the two operations that need
# live AWS -- STS caller identity and Terraform state pull -- with fixture files
# so the fail-closed logic is testable without credentials. Offline runs are
# stamped `evidence_mode = "offline-test"` and can never be mistaken for
# qualified evidence.

set -euo pipefail

# --- exit / reason codes -----------------------------------------------------
#
# Every refusal has a stable reason code. honua-devops#147 binds to these
# strings; do not renumber or rename them casually.

readonly HONUA_IAC_EXIT_USAGE=2
readonly HONUA_IAC_EXIT_REFUSED=3

HONUA_IAC_LAST_REASON=""

log_info() { echo "[INFO] $*"; }
log_warn() { echo "[WARN] $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }

# refuse <reason-code> <message>
# Fails closed before any mutation and prints a machine-greppable reason.
refuse() {
  local reason="$1"
  shift
  HONUA_IAC_LAST_REASON="$reason"
  log_error "REFUSED[$reason]: $*"
  exit "$HONUA_IAC_EXIT_REFUSED"
}

usage_error() {
  log_error "$*"
  exit "$HONUA_IAC_EXIT_USAGE"
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || usage_error "required command not found on PATH: $cmd"
}

# --- terraform binary --------------------------------------------------------

terraform_bin() {
  echo "${HONUA_IAC_TERRAFORM_BIN:-terraform}"
}

tf() {
  "$(terraform_bin)" "$@"
}

terraform_version() {
  tf version -json 2>/dev/null |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null ||
    usage_error "unable to read terraform version from $(terraform_bin)"
}

# version_at_least <have> <want>
version_at_least() {
  python3 - "$1" "$2" <<'PY'
import sys


def parts(v):
    out = []
    for chunk in v.split("-")[0].split("."):
        try:
            out.append(int(chunk))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


sys.exit(0 if parts(sys.argv[1]) >= parts(sys.argv[2]) else 1)
PY
}

# --- canonical JSON and digests ---------------------------------------------

# canonical_json  (stdin JSON -> stdout canonical JSON, no trailing newline)
# Sorted keys, no insignificant whitespace, ASCII-escaped. This is the exact
# byte sequence every digest in the contract is taken over.
canonical_json() {
  python3 -c 'import json,sys; sys.stdout.write(json.dumps(json.load(sys.stdin), sort_keys=True, separators=(",", ":"), ensure_ascii=True))'
}

# json_digest (stdin JSON -> sha256 hex of its canonical form)
json_digest() {
  canonical_json | sha256sum | cut -d' ' -f1
}

sha256_file() {
  local path="$1"
  [[ -f "$path" ]] || refuse "artifact-missing" "expected file not found: $path"
  sha256sum "$path" | cut -d' ' -f1
}

sha256_string() {
  printf '%s' "$1" | sha256sum | cut -d' ' -f1
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- backend identity --------------------------------------------------------

# Non-secret S3/azurerm backend keys that may be recorded verbatim. Anything
# else present in the resolved backend config is recorded as a redacted key
# name only. The complete configuration is hashed separately, so changing a
# redacted value still invalidates the binding without publishing that value.
readonly HONUA_IAC_BACKEND_PUBLIC_KEYS="bucket key region workspace_key_prefix dynamodb_table use_lockfile encrypt kms_key_id role_arn skip_region_validation container_name storage_account_name resource_group_name"

# backend_identity_doc <root> <workspace>
#
# Reads the backend configuration Terraform resolved during `init` from
# <root>/.terraform/terraform.tfstate. That file is authoritative: it already
# folds in every -backend-config override, so an executor cannot smuggle a
# different bucket past the digest by using CLI flags.
backend_identity_doc() {
  local root="$1"
  local workspace="$2"
  local backend_state="$root/.terraform/terraform.tfstate"

  [[ -f "$backend_state" ]] ||
    refuse "backend-uninitialized" "no initialized backend at $backend_state; run terraform init first"

  HONUA_IAC_PUBLIC_KEYS="$HONUA_IAC_BACKEND_PUBLIC_KEYS" \
    python3 - "$backend_state" "$workspace" <<'PY'
import hashlib
import json
import os
import sys

path, workspace = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)

backend = data.get("backend") or {}
kind = backend.get("type") or "local"
config = backend.get("config") or {}
public_keys = set(os.environ["HONUA_IAC_PUBLIC_KEYS"].split())

public = {}
redacted = []
for name, value in sorted(config.items()):
    if value is None:
        continue
    if name in ("assume_role", "assume_role_with_web_identity") and isinstance(value, dict):
        # Session names, external IDs, session policies/tags, and web identity
        # tokens are not evidence. Only the role reference may be published.
        public[name] = {"role_arn": value.get("role_arn")}
        redacted.extend(name + "." + key for key, item in sorted(value.items())
                        if key != "role_arn" and item is not None)
    elif name in public_keys and isinstance(value, (str, bool, int)):
        public[name] = value
    else:
        redacted.append(name)

lock = {"kind": "none", "detail": None}
if kind == "s3":
    if public.get("use_lockfile") is True:
        lock = {"kind": "s3-native-lockfile", "detail": str(public.get("key", "")) + ".tflock"}
    elif public.get("dynamodb_table"):
        lock = {"kind": "dynamodb", "detail": public["dynamodb_table"]}
elif kind == "azurerm":
    lock = {"kind": "azure-blob-lease", "detail": public.get("container_name")}

encryption = {"enabled": None, "kms_key_reference": None}
if kind == "s3":
    encryption = {
        "enabled": public.get("encrypt") is True,
        "kms_key_reference": public.get("kms_key_id"),
    }

assume_role = public.get("assume_role") or public.get("assume_role_with_web_identity")
backend_role_arn = public.get("role_arn")
if not backend_role_arn and isinstance(assume_role, dict):
    backend_role_arn = assume_role.get("role_arn")

bucket = public.get("bucket")
partition = "aws"
doc = {
    "schema_version": "v1",
    "kind": "honua.iac.backend-identity",
    "backend_kind": kind,
    "is_remote": kind not in ("local", ""),
    "workspace": workspace,
    "location": {
        "region": public.get("region"),
        "bucket_id": bucket,
        "bucket_arn": ("arn:%s:s3:::%s" % (partition, bucket)) if bucket else None,
        "object_key": public.get("key"),
        "workspace_key_prefix": public.get("workspace_key_prefix"),
        "container_name": public.get("container_name"),
        "storage_account_name": public.get("storage_account_name"),
    },
    "locking": lock,
    "encryption": encryption,
    "backend_access_role_arn": backend_role_arn,
    "non_secret_config": public,
    "redacted_config_keys": sorted(redacted),
    "resolved_config_digest": hashlib.sha256(json.dumps(
        config, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest(),
    "evidence_scope": "non-secret-backend-configuration",
}
json.dump(doc, sys.stdout, sort_keys=True, separators=(",", ":"))
PY
}

# assert_remote_backend <backend-identity-json>
# The release/certification lane refuses local state outright, and refuses a
# remote backend that cannot name a locking primitive.
assert_remote_backend() {
  local doc="$1"
  local kind is_remote lock_kind
  kind="$(json_get "$doc" backend_kind)"
  is_remote="$(json_get "$doc" is_remote)"
  lock_kind="$(json_get "$doc" locking.kind)"

  if [[ "$is_remote" != "true" ]]; then
    refuse "local-state-refused" \
      "backend kind '$kind' is local state; the release lane requires a remote backend (see docs/operator-state.md)"
  fi

  if [[ "$lock_kind" == "none" ]]; then
    refuse "lock-posture-missing" \
      "remote backend declares no locking primitive; set use_lockfile = true (Terraform >= 1.10) or dynamodb_table"
  fi

  if [[ "$kind" == "s3" ]]; then
    HONUA_IAC_JSON="$doc" python3 -c '
import json, os, sys
location = json.loads(os.environ["HONUA_IAC_JSON"])["location"]
sys.exit(0 if all(isinstance(location.get(key), str) and location[key].strip()
                 for key in ("bucket_id", "object_key", "region")) else 1)
' || refuse "backend-identity-missing" "S3 backend must explicitly identify bucket, object key, and region"
    [[ "$(json_get "$doc" encryption.enabled)" == "true" ]] ||
      refuse "backend-encryption-missing" "S3 backend requires encryption"
    [[ -n "$(json_get "$doc" backend_access_role_arn)" ]] ||
      refuse "backend-role-missing" \
        "S3 backend must assume a backend-access role (assume_role.role_arn); the deployment session must not also be the state writer"
    if HONUA_IAC_JSON="$doc" python3 -c '
import json, os, sys
keys = set(json.loads(os.environ["HONUA_IAC_JSON"])["redacted_config_keys"])
sys.exit(0 if keys.intersection({"access_key", "secret_key", "token", "session_token",
    "sse_customer_key", "assume_role_with_web_identity.web_identity_token"}) else 1)
'; then
      refuse "backend-credential-refused" "backend configuration contains credential material; use short-lived session credentials outside backend configuration"
    fi
  fi
}

# assert_lock_primitive_supported <backend-identity-json> <terraform-version>
assert_lock_primitive_supported() {
  local doc="$1"
  local tf_version="$2"
  local lock_kind
  lock_kind="$(json_get "$doc" locking.kind)"

  if [[ "$lock_kind" == "s3-native-lockfile" ]] && ! version_at_least "$tf_version" "1.10.0"; then
    refuse "lock-primitive-unsupported" \
      "backend requests S3 native lockfile locking but Terraform $tf_version predates use_lockfile (1.10); pin >= 1.10 or use lock_mode = \"dynamodb\""
  fi
}

# --- caller identity ---------------------------------------------------------

# sts_identity_doc
#
# Live mode shells out to the AWS CLI. Offline mode reads a fixture named by
# HONUA_IAC_STS_FIXTURE so the identity-binding checks stay testable without
# credentials. The token, source credential, and session name are never read or
# recorded; only account, partition, role ARN, role id, issuer, and expiry.
sts_identity_doc() {
  if [[ "${HONUA_IAC_OFFLINE:-0}" == "1" ]]; then
    local fixture="${HONUA_IAC_STS_FIXTURE:-}"
    [[ -n "$fixture" && -f "$fixture" ]] ||
      usage_error "offline mode requires HONUA_IAC_STS_FIXTURE to point at a caller-identity fixture"
    python3 - "$fixture" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    raw = json.load(handle)

arn = raw.get("Arn", "")
parts = arn.split(":")
# The credential KIND is still derived from the fixture ARN so the identity
# gates are exercised offline. `evidence_mode` is what keeps an offline run from
# ever presenting as qualified evidence.
kind = "sts-assumed-role" if ":assumed-role/" in arn else (
    "iam-user" if ":user/" in arn else "unknown"
)
doc = {
    "account_id": raw.get("Account"),
    "partition": parts[1] if len(parts) > 2 else "aws",
    "arn": arn,
    "role_id": (raw.get("UserId") or "").split(":")[0] or None,
    "issuer": raw.get("Issuer"),
    "session_expires_at_utc": raw.get("SessionExpiresAtUtc"),
    "credential_kind": kind,
    "evidence_mode": "offline-test",
}
json.dump(doc, sys.stdout, sort_keys=True, separators=(",", ":"))
PY
    return
  fi

  require_cmd aws
  local raw
  raw="$(aws sts get-caller-identity --output json)" ||
    refuse "identity-unavailable" "aws sts get-caller-identity failed; no short-lived session is active"

  HONUA_IAC_STS_RAW="$raw" \
    HONUA_IAC_SESSION_EXPIRY="${HONUA_IAC_SESSION_EXPIRY:-}" \
    HONUA_IAC_IDENTITY_ISSUER="${HONUA_IAC_IDENTITY_ISSUER:-}" \
    python3 - <<'PY'
import json
import os

raw = json.loads(os.environ["HONUA_IAC_STS_RAW"])
arn = raw.get("Arn", "")
parts = arn.split(":")
kind = "sts-assumed-role" if ":assumed-role/" in arn else (
    "iam-user" if ":user/" in arn else "unknown"
)
doc = {
    "account_id": raw.get("Account"),
    "partition": parts[1] if len(parts) > 2 else "aws",
    "arn": arn,
    "role_id": (raw.get("UserId") or "").split(":")[0] or None,
    "issuer": os.environ.get("HONUA_IAC_IDENTITY_ISSUER") or None,
    "session_expires_at_utc": os.environ.get("HONUA_IAC_SESSION_EXPIRY") or None,
    "credential_kind": kind,
    "evidence_mode": "live",
}
print(json.dumps(doc, sort_keys=True, separators=(",", ":")))
PY
}

# backend_roles_separated <backend-json> <identity-json>
# Prints "true" when the caller session and the backend-access role are not the
# same role. STS assumed-role ARNs and IAM role ARNs do not share a string form,
# and assumed-role ARNs drop the IAM path, so the comparison is account plus
# role name. A missing backend role is not "the same" role; that case is
# backend-role-missing.
backend_roles_separated() {
  HONUA_IAC_BACKEND_DOC="$1" HONUA_IAC_IDENTITY_DOC="$2" python3 - <<'PY'
import json
import os


def role_identity(arn):
    if not isinstance(arn, str) or not arn:
        return "", ""
    parts = arn.split(":")
    account = parts[4] if len(parts) > 4 else ""
    name = ""
    if ":assumed-role/" in arn:
        name = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
    elif ":role/" in arn:
        tail = arn.split(":role/", 1)[1].strip("/")
        name = tail.split("/")[-1] if tail else ""
    return account, name


backend = json.loads(os.environ["HONUA_IAC_BACKEND_DOC"])
identity = json.loads(os.environ["HONUA_IAC_IDENTITY_DOC"])
if not isinstance(identity, dict):
    identity = {}
backend_account, backend_name = role_identity(backend.get("backend_access_role_arn") or "")
caller_account, caller_name = role_identity(identity.get("arn") or "")
same = bool(
    backend_name
    and backend_name == caller_name
    and backend_account == caller_account
)
print("false" if same else "true")
PY
}

# assert_backend_role_separated <backend-json> <identity-json>
# The deployment session may assume the deployment role. It must not also be
# the role that reads and writes state.
assert_backend_role_separated() {
  [[ "$(backend_roles_separated "$1" "$2")" == "true" ]] ||
    refuse "backend-role-not-distinct" \
      "caller $(json_get "$2" arn) is the backend-access role $(json_get "$1" backend_access_role_arn); state writes and infrastructure mutation require different roles"
}

# assert_short_lived_identity <identity-json>
# The certified executor federates through STS. A long-lived IAM user principal
# is refused outright unless the caller opts into the explicitly unsupported
# local development posture.
assert_short_lived_identity() {
  local doc="$1"
  local kind
  kind="$(json_get "$doc" credential_kind)"

  case "$kind" in
    sts-assumed-role) return 0 ;;
    iam-user)
      refuse "long-lived-credential-refused" \
        "caller is a long-lived IAM user; the certified lane requires an SSO/OIDC-federated STS session"
      ;;
    *)
      refuse "identity-unrecognized" "unable to classify caller identity as a short-lived STS session"
      ;;
  esac
}

# --- state lineage -----------------------------------------------------------

# state_lineage_doc <root>
#
# Pulls remote state and extracts lineage + serial ONLY. The state document is
# streamed through python and never touches disk, a log, or a receipt.
state_lineage_doc() {
  local root="$1"

  if [[ "${HONUA_IAC_OFFLINE:-0}" == "1" && -n "${HONUA_IAC_STATE_FIXTURE:-}" ]]; then
    python3 - "$HONUA_IAC_STATE_FIXTURE" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    state = json.load(handle)
doc = {"lineage": state.get("lineage"), "serial": state.get("serial", 0)}
json.dump(doc, sys.stdout, sort_keys=True, separators=(",", ":"))
PY
    return
  fi

  tf -chdir="$root" state pull 2>/dev/null | python3 -c '
import json
import sys

raw = sys.stdin.read().strip()
if not raw:
    json.dump({"lineage": None, "serial": 0}, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.exit(0)
state = json.loads(raw)
doc = {"lineage": state.get("lineage"), "serial": state.get("serial", 0)}
json.dump(doc, sys.stdout, sort_keys=True, separators=(",", ":"))
'
}

# --- source pinning ----------------------------------------------------------

# iac_source_doc <repo-root> <terraform-root> <allow-dirty> [explicit-revision]
#
# Binds the exact IaC source: revision, a content digest over every tracked file
# under the Terraform root plus its local module sources, and the module source
# strings themselves. A dirty working tree is a mutable source and is refused
# unless the caller explicitly allows it for local development.
#
# Operators running from the released distribution tarball have no git metadata.
# They supply the release revision explicitly, and the tree digest is then taken
# over the files on disk instead of the git index. Without either a git
# repository or an explicit revision, the source is unpinnable and refused.
iac_source_doc() {
  local repo_root="$1"
  local tf_root="$2"
  local allow_dirty="$3"
  local explicit_revision="${4:-}"

  local revision="unknown"
  local rel_root
  rel_root="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$tf_root" "$repo_root")"

  local -a source_paths=("$rel_root")
  local module_sources=""
  local src
  while IFS= read -r src; do
    [[ -n "$src" ]] || continue
    module_sources+="$src"$'\n'
    if [[ "$src" == ./* || "$src" == ../* ]]; then
      local resolved
      resolved="$(python3 -c 'import os,sys; print(os.path.relpath(os.path.normpath(os.path.join(sys.argv[1], sys.argv[2])), sys.argv[3]))' "$tf_root" "$src" "$repo_root")"
      source_paths+=("$resolved")
    fi
  done < <(grep -hoE '^[[:space:]]*source[[:space:]]*=[[:space:]]*"[^"]+"' "$tf_root"/*.tf 2>/dev/null |
    sed -E 's/.*"([^"]+)".*/\1/' | sort -u)

  local tree_digest="unavailable"
  if git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
    revision="$(git -C "$repo_root" rev-parse HEAD)"
    local dirty
    dirty="$(git -C "$repo_root" status --porcelain -- "${source_paths[@]}" 2>/dev/null || true)"
    if [[ -n "$dirty" && "$allow_dirty" != "true" ]]; then
      refuse "mutable-source" \
        "uncommitted changes under the pinned Terraform source ($rel_root); commit them or pass --allow-dirty-source for disposable local development"
    fi
    tree_digest="$(git -C "$repo_root" ls-files -s -- "${source_paths[@]}" | sort | sha256sum | cut -d' ' -f1)"
  elif [[ -n "$explicit_revision" ]]; then
    revision="$explicit_revision"
    tree_digest="$(
      cd "$repo_root" && find "${source_paths[@]}" -type f \
        -not -path '*/.terraform/*' \
        -not -name '*.tfplan' \
        -not -name '*.tfplan.*' \
        -not -name 'terraform.tfvars' \
        -print0 2>/dev/null |
        sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1
    )"
  elif [[ "$allow_dirty" != "true" ]]; then
    refuse "mutable-source" \
      "Terraform root is not under revision control and no --iac-revision was supplied; the certified lane requires a pinned IaC revision"
  fi

  HONUA_IAC_MODULE_SOURCES="$module_sources" python3 - "$rel_root" "$revision" "$tree_digest" <<'PY'
import json
import os
import sys

sources = [
    line for line in os.environ["HONUA_IAC_MODULE_SOURCES"].splitlines() if line.strip()
]
doc = {
    "terraform_root": sys.argv[1],
    "iac_revision": sys.argv[2],
    "iac_tree_digest": sys.argv[3],
    "module_sources": sorted(sources),
}
json.dump(doc, sys.stdout, sort_keys=True, separators=(",", ":"))
PY
}

# --- inputs ------------------------------------------------------------------

# input_digest_doc <root> <var-file...>
#
# Variable VALUES are hashed, never recorded. The digest still changes when any
# value changes, so an approved plan cannot be re-pointed at different inputs.
input_digest_doc() {
  local root="$1"
  shift
  local entries=""
  local file
  for file in "$@"; do
    [[ -n "$file" ]] || continue
    local abs="$file"
    [[ "$abs" == /* ]] || abs="$root/$file"
    [[ -f "$abs" ]] || refuse "input-missing" "var file not found: $file"
    entries+="varfile:$file=$(sha256_file "$abs")"$'\n'
  done

  local name
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    entries+="env:${name#TF_VAR_}=$(sha256_string "${!name}")"$'\n'
  done < <(compgen -v | grep '^TF_VAR_' | sort || true)

  local auto
  for auto in "$root"/*.auto.tfvars "$root"/*.auto.tfvars.json "$root"/terraform.tfvars; do
    [[ -f "$auto" ]] || continue
    entries+="autofile:$(basename "$auto")=$(sha256_file "$auto")"$'\n'
  done

  HONUA_IAC_INPUT_ENTRIES="$entries" python3 - <<'PY'
import hashlib
import json
import os
import sys

entries = sorted(
    line for line in os.environ["HONUA_IAC_INPUT_ENTRIES"].splitlines() if line.strip()
)
digest = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
doc = {
    "input_digest": digest,
    "input_refs": [entry.split("=", 1)[0] for entry in entries],
}
json.dump(doc, sys.stdout, sort_keys=True, separators=(",", ":"))
PY
}

# provider_lock_digest <root>
provider_lock_digest() {
  local root="$1"
  local lock="$root/.terraform.lock.hcl"
  if [[ -f "$lock" ]]; then
    sha256_file "$lock"
  else
    refuse "provider-lock-missing" \
      "no .terraform.lock.hcl in $root; the certified lane requires an immutably pinned provider set"
  fi
}

# --- one-time claim ----------------------------------------------------------
#
# One plan, one lock holder, one apply. The claim directory is created with
# mkdir (atomic) so a second concurrent executor loses the race deterministically
# instead of both entering apply. A completed claim leaves a receipt marker so a
# replayed plan is refused rather than re-applied.

# claim_acquire <claim-dir> [reclaim-after-seconds]
#
# reclaim-after-seconds > 0 lets an operator recover from an ambiguous client
# disconnect: a claim whose holder never completed and whose acquisition is
# older than the threshold is taken over, loudly. A COMPLETED claim is never
# reclaimed -- that would turn a recovery into a replay.
claim_acquire() {
  local claim_dir="$1"
  local reclaim_after="${2:-0}"

  if mkdir "$claim_dir" 2>/dev/null; then
    printf '%s\n' "$(utc_now)" >"$claim_dir/acquired"
    printf '%s\n' "$$" >"$claim_dir/executor_pid"
    printf '%s\n' "${HONUA_IAC_EXECUTOR_ID:-host-$(hostname)-pid-$$}" >"$claim_dir/executor_id"
    printf '%s\n' "preflight" >"$claim_dir/phase"
    return 0
  fi

  if [[ -f "$claim_dir/completed" ]]; then
    refuse "plan-already-claimed" \
      "this saved plan was already consumed at $(cat "$claim_dir/completed"); regenerate and re-approve a new plan"
  fi

  local phase="unknown" holder_pid=""
  [[ -f "$claim_dir/phase" ]] && phase="$(cat "$claim_dir/phase")"
  [[ -f "$claim_dir/executor_pid" ]] && holder_pid="$(cat "$claim_dir/executor_pid")"

  if [[ "$phase" == "mutation-started" || "$phase" == "terraform-acknowledged" || "$phase" == "reconciliation-required" || "$phase" == "receipt-committed" ]]; then
    refuse "reconciliation-required" \
      "the original executor may have started mutation (phase=$phase); inspect authoritative state and the recovery receipt before continuing"
  fi

  if [[ -n "$holder_pid" ]] && kill -0 "$holder_pid" 2>/dev/null; then
    refuse "concurrent-claim" \
      "the original executor (pid $holder_pid) is still live; reclaim cannot overlap it"
  fi

  if [[ "$reclaim_after" -gt 0 && "$phase" != "preflight" ]]; then
    refuse "reconciliation-required" \
      "this legacy claim has no proof that mutation never started; reconcile it instead of reclaiming"
  fi

  if [[ "$reclaim_after" -gt 0 && -f "$claim_dir/acquired" ]]; then
    local age
    age=$(($(date -u +%s) - $(date -u -r "$claim_dir/acquired" +%s)))
    if [[ "$age" -ge "$reclaim_after" ]]; then
      log_warn "reclaiming a stale claim held for ${age}s without completion: $claim_dir"
      printf '%s\n' "$(utc_now)" >"$claim_dir/acquired"
      printf '%s\n' "$$" >"$claim_dir/executor_pid"
      printf '%s\n' "${HONUA_IAC_EXECUTOR_ID:-host-$(hostname)-pid-$$}" >"$claim_dir/executor_id"
      return 0
    fi
  fi

  refuse "concurrent-claim" \
    "another executor holds the claim for this saved plan ($claim_dir); one plan admits one apply"
}

# claim_status <claim-dir> -> JSON describing the claim without mutating it.
# This is the recoverable-status probe for an ambiguous client disconnect.
claim_status() {
  local claim_dir="$1"
  local state="free" acquired="" completed="" phase="" executor_id="" receipt_digest=""
  if [[ -f "$claim_dir/completed" ]]; then
    state="completed"
    completed="$(cat "$claim_dir/completed")"
    [[ -f "$claim_dir/acquired" ]] && acquired="$(cat "$claim_dir/acquired")"
  elif [[ -d "$claim_dir" ]]; then
    state="held"
    [[ -f "$claim_dir/acquired" ]] && acquired="$(cat "$claim_dir/acquired")"
  fi
  [[ -f "$claim_dir/phase" ]] && phase="$(cat "$claim_dir/phase")"
  [[ "$phase" == "mutation-started" || "$phase" == "terraform-acknowledged" || "$phase" == "receipt-committed" || "$phase" == "reconciliation-required" ]] && state="reconciliation-required"
  [[ -f "$claim_dir/executor_id" ]] && executor_id="$(cat "$claim_dir/executor_id")"
  [[ -f "$claim_dir/receipt.json" ]] && receipt_digest="$(sha256_file "$claim_dir/receipt.json")"
  HONUA_IAC_CLAIM_STATE="$state" \
    HONUA_IAC_CLAIM_ACQUIRED="$acquired" \
    HONUA_IAC_CLAIM_COMPLETED="$completed" \
    HONUA_IAC_CLAIM_PHASE="$phase" \
    HONUA_IAC_EXECUTOR_ID="$executor_id" \
    HONUA_IAC_RECEIPT_DIGEST="$receipt_digest" \
    HONUA_IAC_CLAIM_DIR="$claim_dir" \
    python3 - <<'PY'
import json
import os
import sys

json.dump(
    {
        "schema_version": "v1",
        "kind": "honua.iac.claim-status",
        "claim_dir": os.environ["HONUA_IAC_CLAIM_DIR"],
        "state": os.environ["HONUA_IAC_CLAIM_STATE"],
        "acquired_at_utc": os.environ["HONUA_IAC_CLAIM_ACQUIRED"].strip() or None,
        "completed_at_utc": os.environ["HONUA_IAC_CLAIM_COMPLETED"].strip() or None,
        "phase": os.environ["HONUA_IAC_CLAIM_PHASE"].strip() or None,
        "executor_id": os.environ["HONUA_IAC_EXECUTOR_ID"].strip() or None,
        "receipt_digest": os.environ["HONUA_IAC_RECEIPT_DIGEST"].strip() or None,
    },
    sys.stdout,
    sort_keys=True,
    separators=(",", ":"),
    indent=2,
)
sys.stdout.write("\n")
PY
}

claim_complete() {
  local claim_dir="$1"
  printf '%s\n' "$(utc_now)" >"$claim_dir/completed"
}

claim_phase() {
  local claim_dir="$1" phase="$2" temporary
  temporary="$claim_dir/.phase.$$"
  printf '%s\n' "$phase" >"$temporary"
  mv "$temporary" "$claim_dir/phase"
}

# Drop a claim that never reached execution so an operator can fix the cause and
# retry. A completed claim is never removed -- that record is what makes a
# replayed plan detectable.
claim_abandon() {
  local claim_dir="$1"
  [[ -f "$claim_dir/completed" ]] && return 0
  if [[ -f "$claim_dir/phase" && "$(cat "$claim_dir/phase")" != "preflight" ]]; then
    claim_phase "$claim_dir" "reconciliation-required"
    return 0
  fi
  rm -rf "$claim_dir"
}

# --- expiry ------------------------------------------------------------------

assert_not_expired() {
  local expires_at="$1"
  python3 - "$expires_at" <<'PY' || exit $?
import datetime
import sys

raw = sys.argv[1]
try:
    expires = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
except ValueError:
    print("[ERROR] REFUSED[plan-expiry-unparseable]: %s" % raw, file=sys.stderr)
    sys.exit(3)

if datetime.datetime.now(datetime.timezone.utc) >= expires:
    print(
        "[ERROR] REFUSED[plan-expired]: saved plan expired at %s" % raw,
        file=sys.stderr,
    )
    sys.exit(3)
PY
}

# --- comparison --------------------------------------------------------------

# assert_equal <reason-code> <label> <expected> <actual>
assert_equal() {
  local reason="$1"
  local label="$2"
  local expected="$3"
  local actual="$4"
  if [[ "$expected" != "$actual" ]]; then
    refuse "$reason" "$label mismatch: plan bound '$expected' but the execution context reports '$actual'"
  fi
}

json_get() {
  # json_get <json> <dotted.path>
  HONUA_IAC_JSON="$1" python3 - "$2" <<'PY'
import json
import os
import sys

data = json.loads(os.environ["HONUA_IAC_JSON"])
for part in sys.argv[1].split("."):
    if data is None:
        break
    data = data.get(part) if isinstance(data, dict) else None
if data is None:
    print("")
elif isinstance(data, bool):
    print("true" if data else "false")
else:
    print(data)
PY
}
