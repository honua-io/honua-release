This synthetic response projects the maintained credential-free custom-code Batch
pool in `honua-iac` at revision `99f9f68afaa3f8b8233656953ba3987f9505d861`,
`infrastructure/terraform/modules/aws-serverless/customcode-batch.tf`, source SHA256
`5abd998c04710677863ff01491c0ea481ec892234a9316755e44251204e2e885`.
Account, region, names and digest are fixture substitutions, not provider receipts.

The source uses Fargate, user1001, the execution role, vCPU1/memory2048 defaults,
private networking, Linux runtime, empty definition environment/secrets, awslogs,
one attempt and3600-second timeout. Tiers m/l/xl add50/100/200GiB of scratch storage;
tier s omits the storage block. This test preserves those settings and only
DESCRIBES the revision. The native server separately supplies job-scoped broker
credentials/parameters at submission; those are not baked definition credentials.
No AWS launch, image execution, cost settlement or installed qualification is claimed.
