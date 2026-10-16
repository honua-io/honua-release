"""Isolated immutable-image entrypoint for a single MCP action."""
import sys

sys.path.insert(0, '/app')
from agent_e2e_mcp_runner import main

if __name__ == '__main__':
    raise SystemExit(main())
