"""Immutable image entrypoint; use Python -I to exclude workspace imports."""
import sys

sys.path.insert(0, "/app")
from agent_e2e_container import main

if __name__ == "__main__":
    main()
