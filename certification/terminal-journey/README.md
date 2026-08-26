# Terminal-first control-plane certification

This directory owns the deterministic, model-free terminal journey and the control-plane roster
drift gate. The primary 2026.1 workspace is one terminal using exact installed client artifacts;
Console and browser Studio remain separate client receipts and do not define this roster.

`run.py` always materializes all eight numbered journey stages. In build-only mode they are blocked,
with the upstream issue and command preserved at each stage. It cannot claim journey success from a
harness build. Once server #3363 publishes the authoritative Admin OpenAPI/CLI and MCP projection
exports, pass both files to `--rest-roster` and `--mcp-roster`; the gate requires an exact 396 = 385
+ 11 partition, unique IDs, and no overlap. Secret/session exclusions stay REST/CLI-only and use a
private secret sink. Anonymous MCP discovery never implies call authorization.

The remaining local live run depends on server #3411, #3430, #3431, #3474 and #3475. AWS wrapping
and genuine-model evidence are linked as #129 and #161, never embedded or treated as substitutes.
