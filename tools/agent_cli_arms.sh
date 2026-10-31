#!/usr/bin/env bash
set -euo pipefail
exec "${PYTHON:-python3}" "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/agent_cli_arms.py" "$@"
