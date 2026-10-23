# Foundation deployment approval

The foundation workflow checks the versioned policy in
`foundation-environment.json` before starting its credential-bearing job. A
missing environment, different reviewer identity or additional deployment branch
refuses execution. This check runs before GitHub can implicitly create an
unprotected environment for the deployment job.

After the release implementation is reviewed and merged, run
`hosted-pilot-foundation-environment.yml` on release trunk. It uses the existing
release GitHub administration credential to create the declared environment and
its exact trunk branch policy. It verifies the result and refuses to overwrite
an existing different protection policy. Configuration stays in source and the
checked-in workflow; no manual repository or AWS configuration is required.

Plan mode uses the existing ephemeral environment. Deploy mode uses
`terraform-live-approval`, whose declared reviewer is Mike McDougall. The owner
can review and approve a dispatch they initiated; the assistant does not approve
or bypass that review. Prepare the concrete configuration and plan metadata
before requesting deployment approval.

The source-reviewed bootstrap apply step has a 30-minute limit within a
45-minute job. Later recovery-journal and artifact steps retain time after an
apply timeout. Recovery can identify the original producer run without an
Actions artifact; consult the bootstrap recovery instructions before retrying.
