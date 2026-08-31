# Certification receipt v2 producer contract

`honua.certification-evidence-receipt/v2` is the only receipt schema accepted by the
certification consumer. Receipt v2 is producer-strict: the producer must receive the governed
requirement context and embed it in the receipt identity before hashing and publishing the receipt.
The consumer does not infer, join, default, or repair that context. There is no v1 grace window.

In addition to the existing receipt identity, every producer must emit these exact fields:

| field | producer obligation |
|---|---|
| `maturity` | Copy the governed maturity for the exact certification cell. |
| `required_tier` | Copy the governed minimum tier for the exact certification cell. |
| `requirements_revision` | Copy the revision of the governed requirements input used for the run. |

The values are identity, not annotations. They are included in the canonical receipt bytes and
therefore in `evidence_digest`. A producer must fail without publishing a receipt if the governed
input is missing, ambiguous, or does not contain the exact cell. It must not derive the values from
the result ledger, use repository defaults, or accept untrusted dispatch strings without provenance
to the governed requirements revision.

The consumer rejects a receipt when its schema is not v2, any context field is absent or extra
identity fields are present, either cell value differs, or `requirements_revision` differs from the
repository-owned revision. Rejection is fail-closed at every evaluation tier; a context-less receipt
cannot be made valid by consumer-side binding.

This contract composes with the truthful execution-identity contract tracked by
[honua-evidence#46](https://github.com/honua-io/honua-evidence/issues/46) and implemented by
[honua-evidence#49](https://github.com/honua-io/honua-evidence/pull/49). Producers must satisfy both
contracts in the same observation path; receipt v2 does not replace the seven truthful-identity
fields enforced during evidence intake.

## Producer migration ledger

All rows below are expected to remain red until their linked migration lands and a fresh receipt is
produced. An earlier truthful-identity migration or initial producer build does not imply receipt-v2
compliance.

| producer | receipt-v2 owner | related truthful-identity work | rollout state |
|---|---|---|---|
| honua-server protocol harness, client-interop, CITE, and CNG | [honua-server#3753](https://github.com/honua-io/honua-server/issues/3753) | [honua-server#3381](https://github.com/honua-io/honua-server/issues/3381), [honua-server#3481](https://github.com/honua-io/honua-server/pull/3481) | migration required |
| honua-sdk-js certification fragments | [honua-sdk-js#1567](https://github.com/honua-io/honua-sdk-js/issues/1567) | [honua-sdk-js#1521](https://github.com/honua-io/honua-sdk-js/pull/1521) | migration required |
| honua-sdk-python conformance fragments | [honua-sdk-python#216](https://github.com/honua-io/honua-sdk-python/issues/216) | [honua-sdk-python#213](https://github.com/honua-io/honua-sdk-python/pull/213) | migration required |
| honua-sdk-dotnet certification fragments | [honua-sdk-dotnet#323](https://github.com/honua-io/honua-sdk-dotnet/issues/323) | [honua-sdk-dotnet#317](https://github.com/honua-io/honua-sdk-dotnet/pull/317) | migration required |
| geospatial-grpc certification fragments | [geospatial-grpc#99](https://github.com/honua-io/geospatial-grpc/issues/99) | [geospatial-grpc#91](https://github.com/honua-io/geospatial-grpc/pull/91) | migration required |
| geospatial-mcp certification fragments | [geospatial-mcp#81](https://github.com/honua-io/geospatial-mcp/issues/81) | [geospatial-mcp#79](https://github.com/honua-io/geospatial-mcp/pull/79), [geospatial-mcp#80](https://github.com/honua-io/geospatial-mcp/pull/80) | migration required |
| honua-esri-compat matrix fragments | [honua-esri-compat#98](https://github.com/honua-io/honua-esri-compat/issues/98) | [honua-esri-compat#75](https://github.com/honua-io/honua-esri-compat/issues/75) | migration required; licensed runner also open |
| honua-evidence `client-interop-cert-v1` normalizer | [honua-evidence#51](https://github.com/honua-io/honua-evidence/issues/51) | [honua-evidence#44](https://github.com/honua-io/honua-evidence/pull/44) | migration required |

The governing rollout decision is
[honua-release#182](https://github.com/honua-io/honua-release/issues/182), with the structural v2
landing in [honua-release#208](https://github.com/honua-io/honua-release/pull/208).
