run_concurrent_start() (
  # Subshell EXIT cleanup covers every return, including failed container creation/assertions.
  local holder_log="$OUT/advisory-lock.log" a b i ready=0 failed=0
  a="${PROJECT}_chaos_app_a"
  b="${PROJECT}_chaos_app_b"
  trap 'docker rm -f "$a" "$b" >/dev/null 2>&1 || true; [ -z "$LOCK_HOLDER_PID" ] || kill "$LOCK_HOLDER_PID" >/dev/null 2>&1 || true' EXIT
  restore_baseline
  export HONUA_SERVER_IMAGE="$CANDIDATE_IMAGE"
  db_exec "SELECT pg_advisory_lock($LOCK_KEY); SELECT pg_sleep(90);" > "$holder_log" 2>&1 &
  LOCK_HOLDER_PID=$!
  sleep 2
  docker rm -f "$a" "$b" >/dev/null 2>&1 || true
  compose run -d --no-deps --name "$a" server >/dev/null || return 1
  compose run -d --no-deps --name "$b" server >/dev/null || return 1
  for i in $(seq 1 "${CHAOS_READY_ATTEMPTS:-90}"); do
    docker logs "$a" > "$OUT/concurrent-a.log" 2>&1 || return 1
    docker logs "$b" > "$OUT/concurrent-b.log" 2>&1 || return 1
    if grep -Eiq 'migration failed' "$OUT/concurrent-a.log" "$OUT/concurrent-b.log"; then
      failed=1
      break
    fi
    if container_ready "$a" && container_ready "$b"; then
      ready=1
      break
    fi
    sleep 2
  done
  kill "$LOCK_HOLDER_PID" >/dev/null 2>&1 || true
  wait "$LOCK_HOLDER_PID" >/dev/null 2>&1 || true
  LOCK_HOLDER_PID=""
  if [ "$failed" = 0 ] && [ "$ready" = 1 ] && wait_for_journal; then
    if ! assert_state concurrent-app-start; then
      return 1
    fi
  else
    record concurrent-app-start fail "both concurrent starters must be healthy without migration failures and converge"
    return 1
  fi
)
