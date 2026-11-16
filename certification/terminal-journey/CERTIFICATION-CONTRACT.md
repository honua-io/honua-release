# Terminal certification contract

`certification.schema.json` defines the terminal setup-to-published-map
certification envelope. `certification-contract-input.json` is a static,
non-qualification input with synthetic candidate and receipt identities.
`test_certification.py` checks the envelope and refuses promotion when stages,
independent gates, or required outcomes are incomplete.

A live certification must collect a fresh receipt bound to the exact candidate
and installed client bytes. Complete the ordered terminal journey in [README.md](README.md),
including separate-principal approval and final URL verification. Retain the
source receipt and its content hash together with the stage observations.
Schema validation alone does not establish an installed-product pass.
