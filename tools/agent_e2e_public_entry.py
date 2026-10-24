"""Fixed isolated public-input startup command in the reviewed worker image."""
import json
import sys

sys.path.insert(0, '/app')
from agent_e2e_public import prepare

if __name__ == '__main__':
    try:
        print(json.dumps(prepare(), sort_keys=True, allow_nan=False))
    except Exception:
        raise SystemExit(1)  # Never print private process state or exception data.
