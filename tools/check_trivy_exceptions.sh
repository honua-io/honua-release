#!/usr/bin/env bash
# Validate the Trivy exception ledger (.trivyignore.yaml, given as JSON) and print it as a
# Markdown table. Exits non-zero when the ledger is empty or any entry is not one exact path
# with a reason, owner, review point and follow-up (founder decision D1, 2026-10-26).
set -euo pipefail
ledger="${1:?usage: check_trivy_exceptions.sh <trivyignore.json>}"
jq -e '(.misconfigurations | type) == "array" and (.misconfigurations | length) > 0' "$ledger" >/dev/null \
  || { echo "::error::exception ledger has no misconfigurations entries" >&2; exit 1; }
echo "### Accepted Trivy exceptions (.trivyignore.yaml)"
echo "| id | path | statement |"
echo "|---|---|---|"
jq -r '.misconfigurations[] | "| \(.id) | `\(.paths[0] // "")` | \(.statement // "") |"' "$ledger"
bad=$(jq -r '.misconfigurations[]
  | select(((.paths // []) | length) != 1
      or ((.paths[0] // "") | test("[*\\[]|^$|/$"))
      or ((.statement // "") | test("Reason: ") | not)
      or ((.statement // "") | test("Owner: ") | not)
      or ((.statement // "") | test("Review: ") | not)
      or ((.statement // "") | test("Follow-up: ") | not))
  | "\(.id) \(.paths // [] | tostring)"' "$ledger")
if [ -n "$bad" ]; then
  echo "::error::exception entries need one exact file path and Reason/Owner/Review/Follow-up: $bad" >&2
  exit 1
fi
