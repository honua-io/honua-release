## Executable docs — fail

Candidate `ghcr.io/honua-io/honua-server:nightly-93cad86@sha256:785de2ae7f72e6277ce62aa1ece4f08ee0425970b71c47f9f4843c0baae2c8db` · {'fail': 1, 'pass': 4}

| document | revision | status | pass | fail | needs-input | blocked | not-run |
|---|---|---|---|---|---|---|---|
| [honua-sdk-js:README.md](https://github.com/honua-io/honua-sdk-js/blob/c6484209dda9b1533fefcae1b0e47d87b56414c2/README.md) | `c6484209` | **pass** | 9 | 0 | 0 | 0 | 3 |
| [honua-sdk-js:INSTALL.md](https://github.com/honua-io/honua-sdk-js/blob/c6484209dda9b1533fefcae1b0e47d87b56414c2/INSTALL.md) | `c6484209` | **pass** | 3 | 0 | 0 | 0 | 0 |
| [honua-sdk-js:docs/quickstart.md](https://github.com/honua-io/honua-sdk-js/blob/c6484209dda9b1533fefcae1b0e47d87b56414c2/docs/quickstart.md) | `c6484209` | **fail** | 3 | 2 | 0 | 0 | 1 |
| [honua-sdk-js:mcp/README.md](https://github.com/honua-io/honua-sdk-js/blob/46ecd25a32ee2bfb7741ccc14ee5842c8824dd19/mcp/README.md) | `46ecd25a` | **pass** | 16 | 0 | 0 | 0 | 4 |
| [honua-sdk-js:packages/create-honua-app/README.md](https://github.com/honua-io/honua-sdk-js/blob/db2fcdebb1446ef636361027e67ecd2971108a3e/packages/create-honua-app/README.md) | `db2fcdeb` | **pass** | 3 | 0 | 0 | 0 | 1 |

### Blocks requiring attention

- `honua-sdk-js:docs/quickstart.md` block 4 (line 127, shell): **fail** — exit code 128; source checkout or lock changed during build
- `honua-sdk-js:docs/quickstart.md` block 5 (line 135, shell): **fail** — exit code 1; source checkout or lock changed during build
