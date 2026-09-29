# shellcheck shell=bash
# Assemble the Slice-1 gate-report.json from the per-driver fragment files.
#
# Contract (the AI/MCP layer parses this, never logs — RELEASE plan §15):
#   {
#     gate, status, require_real, generatedAt, server:{image,booted},
#     boot:{failed,reason,detail,exitCode,containerState,errorLines[],allScenariosBlocked}
#     summary:{pass,fail,blocked,skipped},
#     scenarios[]        : {scenario,status,why,evidence}      (S0 boot row + S1..S9)
#     protocolCoverage[] : {protocol,operation,status,detail}  (three-protocol parity)
#     formatCoverage[]   : {format,read,write,roundtrip,notes} (the owner's format cut)
#   }
#
# Verdict (mechanical, not a judgement — AGENTS.md):
#   - the candidate stack DID NOT BOOT => overall fail on EVERY trigger, require_real or not
#     (honua-release#303). "Did not boot" is: boot.sh recorded booted=false (the health probe never
#     returned 200 / the image never pulled), or every scenario is blocked/skipped citing the boot.
#     A gate that reports pass for a stack that never served a request is not honest-BLOCKED, it is
#     wrong: there is no evidence either way, so it cannot be green.
#   - a `fail` in any scenario  => overall fail.
#   - a scenario-level `blocked`/`skipped` (a missing dep on an otherwise live stack) is tolerated
#     per-PR but is promoted to fail when E2E_REQUIRE_REAL is set (nightly / release-train real cut).
#     That is what require_real still means; the boot rule above is independent of it.
#   - format read/roundtrip `fail` => overall fail; `na`/`blocked`/`skipped` never fail by themselves
#     (a read-only format legitimately has write:"na").
set -euo pipefail

assemble_report() { # out-dir  -> writes out-dir/gate-report.json, returns non-zero if overall != pass
  local out="$1"
  local require_real="${E2E_REQUIRE_REAL:-}"
  local image="${E2E_SERVER_IMAGE:-unknown}"
  : > "$out/scenarios.jsonl.f"; : > "$out/protocols.jsonl.f"; : > "$out/formats.jsonl.f"
  [ -f "$out/scenarios.jsonl" ] && cp "$out/scenarios.jsonl" "$out/scenarios.jsonl.f"
  [ -f "$out/protocols.jsonl" ] && cp "$out/protocols.jsonl" "$out/protocols.jsonl.f"
  [ -f "$out/formats.jsonl"   ] && cp "$out/formats.jsonl"   "$out/formats.jsonl.f"

  local rr="false"; [ -n "$require_real" ] && rr="true"
  # Normalise here so a stray value can never make jq --argjson explode mid-gate.
  local booted="false"
  case "${E2E_SERVER_BOOTED:-false}" in true|1|yes) booted="true" ;; esac

  # boot.json is written by boot.sh (reason + container exit code + first error lines). When it is
  # absent — an older harness, or a crash before boot ran — fall back to E2E_SERVER_BOOTED.
  local boot_json="null"
  if [ -f "$out/boot.json" ] && jq -e 'type == "object"' "$out/boot.json" >/dev/null 2>&1; then
    boot_json="$(cat "$out/boot.json")"
  fi

  jq -n \
    --slurpfile scen "$out/scenarios.jsonl.f" \
    --slurpfile proto "$out/protocols.jsonl.f" \
    --slurpfile fmt "$out/formats.jsonl.f" \
    --argjson require_real "$rr" \
    --argjson booted "$booted" \
    --argjson boot "$boot_json" \
    --arg image "$image" \
    --arg ts "$(date -u +%FT%TZ)" '
    ($boot // {}) as $b
    # boot.sh is authoritative about whether the stack came up; E2E_SERVER_BOOTED is the fallback.
    | (if ($b.booted | type) == "boolean" then $b.booted else $booted end) as $bootedFlag
    # Second signal, for a stack that answered nothing: every scenario blocked/skipped on the boot
    # itself. That is a dead candidate, not a set of independently-missing deps.
    | (($scen | length) > 0
       and all($scen[]; .status == "blocked" or .status == "skipped")
       and any($scen[]; (.why // "") | test("server not ready|did not boot|never became ready|not reachable"; "i"))
      ) as $allBlockedOnBoot
    | (($bootedFlag | not) or $allBlockedOnBoot) as $bootFailed
    | {
        failed: $bootFailed,
        reason: (if $bootFailed
                 then ($b.reason // (if ($bootedFlag | not) then "server-never-ready" else "all-scenarios-blocked-on-boot" end))
                 else null end),
        detail: (if $bootFailed
                 then ($b.detail // "no scenario could reach the candidate stack")
                 else null end),
        image: ($b.image // $image),
        healthUrl: ($b.healthUrl // null),
        exitCode: ($b.exitCode // null),
        containerState: ($b.containerState // null),
        errorLines: ($b.errorLines // []),
        allScenariosBlocked: $allBlockedOnBoot
      } as $bootInfo
    # The boot failure gets its OWN report row so it shows up in the scenarios table and in
    # summary.fail, instead of being inferable only from 13 identical BLOCKED rows.
    | (if $bootFailed then [{
        scenario: "S0-stack-boot",
        status: "fail",
        why: ("candidate stack did not boot: " + $bootInfo.reason
              + " (container exit code " + (($bootInfo.exitCode // "unknown") | tostring)
              + ", state " + (($bootInfo.containerState // "unknown") | tostring) + ")"
              + (if ($bootInfo.errorLines | length) > 0 then " — " + $bootInfo.errorLines[0] else "" end)),
        evidence: {
          image: $bootInfo.image,
          healthUrl: $bootInfo.healthUrl,
          exitCode: $bootInfo.exitCode,
          containerState: $bootInfo.containerState,
          errorLines: $bootInfo.errorLines,
          detail: $bootInfo.detail
        }
      }] else [] end) as $bootRows
    | ($bootRows + $scen) as $rows
    | {
      gate: "e2e-local-docker",
      require_real: $require_real,
      generatedAt: $ts,
      server: { image: $image, booted: $bootedFlag },
      boot: $bootInfo,
      scenarios: $rows,
      protocolCoverage: $proto,
      formatCoverage: $fmt,
      summary: {
        pass:    ([$rows[]|select(.status=="pass")]|length),
        fail:    ([$rows[]|select(.status=="fail")]|length),
        blocked: ([$rows[]|select(.status=="blocked")]|length),
        skipped: ([$rows[]|select(.status=="skipped")]|length)
      }
    }
    | .blockedCount = ([.scenarios[]?|select(.status=="blocked" or .status=="skipped")]|length)
    | .status = (
        # A scenario fail is a real regression and always fails the gate (the S8 scenario itself
        # fails on a CORE-format regression). A stack that never booted always fails the gate too,
        # on every trigger: there is no evidence to be green about. Individual formatCoverage
        # read/roundtrip gaps on exotic formats are surfaced for the owner cut but do not
        # independently block every platform PR. Per-PR, scenario-level BLOCKED/SKIPPED on a LIVE
        # stack are tolerated (honest while a dep is missing); E2E_REQUIRE_REAL (nightly / real cut)
        # promotes them to fail.
        if   .boot.failed                            then "fail"
        elif any(.scenarios[]?; .status=="fail")     then "fail"
        elif ($require_real and (.blockedCount > 0)) then "fail"
        else "pass" end)
    ' > "$out/gate-report.json"

  rm -f "$out"/*.jsonl.f
  report_boot_failure "$out/gate-report.json"
  local overall; overall="$(jq -r .status "$out/gate-report.json")"
  echo "overall e2e status: $overall"
  [ "$overall" = "pass" ]
}

# report_boot_failure — annotate a non-booting candidate for humans: a GitHub error annotation plus,
# when running in Actions, its own job-summary block naming the exit code and the first error lines.
report_boot_failure() { # gate-report.json
  local f="$1"
  jq -e '.boot.failed' "$f" >/dev/null 2>&1 || return 0
  local reason code state
  reason="$(jq -r '.boot.reason // "unknown"' "$f")"
  code="$(jq -r '.boot.exitCode // "unknown"' "$f")"
  state="$(jq -r '.boot.containerState // "unknown"' "$f")"
  echo "::error:: slice1 FAIL — candidate stack did not boot ($reason): container exit code ${code}, state ${state}. This fails the gate on every trigger, require_real or not."
  jq -r '.boot.errorLines[]? | "  | " + .' "$f"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "### :rotating_light: candidate stack did not boot — slice1 FAIL"
      echo ""
      echo "\`$(jq -r '.boot.image' "$f")\` never served \`$(jq -r '.boot.healthUrl // "/healthz/ready"' "$f")\`."
      echo ""
      echo "| reason | container exit code | container state |"
      echo "|---|---|---|"
      echo "| $reason | $code | $state |"
      echo ""
      echo "A candidate that does not boot fails this gate on every trigger; \`require_real\` only governs scenario-level BLOCKED."
      local lines; lines="$(jq -r '.boot.errorLines[]?' "$f")"
      if [ -n "$lines" ]; then
        echo ""
        echo "first error lines:"
        echo '```'
        echo "$lines"
        echo '```'
      fi
    } >> "$GITHUB_STEP_SUMMARY"
  fi
}
