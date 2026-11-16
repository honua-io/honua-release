# MCP contract inputs

`mcp-tool-contract-input.json` and `mcp-query-contract-input.json` are static
regression inputs for `mcp_contract_controls.py`. Dates and opaque digests are
substituted consistently; JSON text and structured payloads retain their matching
values and wire shapes. Source IDs, attributes, result counts, status values,
summary boundaries and invalid mutations remain covered by the controls.

These files are not execution receipts or installed-product qualification evidence.
Live checks must collect fresh, candidate-bound observations.
