# Release exceptions

`release-exceptions.json` is the active policy input for publisher-attestation exceptions,
protocol Preview producers, the Preview Lambda image, and single-tenant GA journeys.
It retains the exact artifact digests, producer names, cells and owners consumed by those gates.
The gates enforce each exception's existing scope and expiry conditions; this file is not proof
that a release or journey passed. Historical decision inputs and narrative records are not required
by these consumers.
