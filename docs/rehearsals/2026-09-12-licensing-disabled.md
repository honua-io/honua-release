# 2026.1 disabled-licensing gate rehearsal

Observed 2026-09-12, release#338. This is a **negative candidate observation**, not
qualification of a newer image or a full DR receipt.

The isolated local Docker compose (`release-338-license-proof`, port 18338) booted
the manifest-pinned published image with `Licensing__Mode=Disabled`, no license
file and no development edition grant:

- Server source: `7ba422672e0c751843b17beb36e954a019cc19fb`.
- Image: `ghcr.io/honua-io/honua-server:nightly-aot-7ba4226`.
- Digest: `sha256:dd50cd81c057e37e73a6144572abdfc90d48de314d7625c54c4ef3b6eb65b0fd`.
- Readiness: healthy.
- Authenticated `GET /api/v1/admin/license`: HTTP 200, `edition: Community`,
  `isValid: true`, `validationState: NoLicenseConfigured`; **no `mode` field**.
- `e2e/licensing.py`: exit nonzero, `2026.1 requires admin license mode: disabled
  (missing/enabled is non-passing)`.
- Test containers, network and volumes were removed after the observation.

The expected value comes from the operator ruling and server#4721's public API
contract (`data.mode == "disabled"`), not from a snapshot of this old image's output.
The negative observation proves an image that ignores the new configuration cannot
be certified by health or a valid Community status. Unit checks also reject enabled,
malformed, unauthorized and missing-mode responses and verify authenticated cloud
parity has no bootstrap exemption.

[server#4725](https://github.com/honua-io/honua-server/pull/4725) implemented the new
mode after this pin. The exact-candidate acceptance criterion is released from the
pre-cut implementation PR: manufacture/re-pin a candidate containing that change,
then run the train without license inputs and require the local Docker, cloud,
installed-client, terminal and DR runtime assertions to pass. This receipt does not
claim that rerun, a passing capacity soak, or a fresh full-platform DR receipt.

The implementation retains #334's full-platform substrate inventory, destructive
restore checks, signatures, attestations and receipt validation. Licensing is
checked before seeding and after the recovered server becomes healthy.
