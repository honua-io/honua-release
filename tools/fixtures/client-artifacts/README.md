# Synthetic client artifact fixtures

These offline fixtures exercise package identity verification and candidate
resolution. They are not registry responses, publication evidence, or SDK builds.
The names, versions and source revisions provide test inputs only.

The npm tarball, Python wheel and NuGet archives contain only the minimal package
metadata read by the verifier. No SDK implementation is included. NuGet catalog
and registration documents are synthetic JSON. `urls.json` maps mocked HTTPS
requests to local bodies; tests reject unexpected requests instead of using a
live registry.

The synthetic Python provenance contains a signed DSSE statement and a self-signed
test certificate with repository and source extensions. Its ephemeral private key
was discarded. The certificate is not a production trust anchor or proof of a
registry publication. Tests retain signature, subject, source, digest and archive
tamper checks. Expected byte digests are fixed in the test assertions and pins.
