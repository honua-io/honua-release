# Pilot human enrollment

Use this path after the isolated Cognito pool exists and the operator has
nominated real people. This prepares access for actual Console/Studio sessions;
creating accounts does not qualify those sessions or the release demo.

Cognito account and group changes use `hosted-pilot-deploy.yml` and its reviewed
saved plan. Native Server memberships use the separately reviewed private task
described below. Do not use AWS CLI/Console account creation, password changes or
group edits. The sealing helper performs resource reads and KMS encryption only.

## Prepare the nominated identities

1. Keep initial image preparation unchanged. In a separate reviewed application
   bootstrap configuration, enable `enable_human_enrollment` and deploy it through
   `honua-iac`'s `hosted-pilot-bootstrap.yml`. This grants plan/apply only the
   required reads; only apply gets account and membership writes, restricted to
   this pilot's tagged user pool. Runtime agents receive none of these grants.
2. Obtain the actual pool ID and original state-custody KMS ARN from the existing
   deployment's private output and foundation bindings. Never substitute a demo
   pool, inferred subject, newly created key or unobserved placeholder.
3. In an owner-only file **outside the repository**, record two to ten nominated
   people under opaque aliases. Each person has exactly `email`, `role` (either
   `authors` or `approvers`), and `emailOwnershipConfirmed: true`. The operator
   must actually confirm control of that email before asserting it. Distinct
   people must use distinct emails; a separate author and approver are required.
   Do not add passwords to this input. Its mode must be `0600`.
4. From the reviewed release checkout, run the helper with the authorized
   operator's existing AWS identity. It needs STS identity, Cognito describe/tag
   reads, KMS describe and encryption on the exact custody key. No new credential
   or identity is provisioned by this command. Use actual values for the shell
   variables; keep the nominated-person input and private handoff off logs.

```sh
python3 tools/hosted_pilot/human_enrollment.py \
  --private-input "$PILOT_PRIVATE_PEOPLE" \
  --name-prefix "$PILOT_NAME_PREFIX" \
  --pool-id "$PILOT_COGNITO_POOL_ID" \
  --key-arn "$PILOT_STATE_KMS_ARN" \
  --sealed-output "$PILOT_SEALED_ENROLLMENT" \
  --private-handoff "$PILOT_PRIVATE_HANDOFF"
```

The helper verifies the observed account, isolated admin-only pool, ownership
tags and enabled symmetric key before encrypting. Each ciphertext binds the
pilot, pool, alias and role. It generates a different strong temporary password
per person and writes two new owner-only files without overwriting an earlier
handoff. It sends no invitations and creates no accounts. If writing either
output fails, preserve any existing private handoff and resolve the failure
before using new output; do not assume an account was created.

## Review and apply

Copy only the sealed output object into the tracked application configuration's
`human_enrollment` field (or `application.human_enrollment` in the campaign
configuration). Its fields are `pool_id`, `key_arn` and `users`; each
user contains only `role` and `payload`. Do not commit either private input or
the handoff. The deployment helper refuses plaintext/unknown nested fields and
any key other than the original state-custody key before Terraform actions.

Review the configuration and use the existing workflow's plan/apply sequence.
Terraform decrypts within that workflow, checks the exact managed pool and
distinct confirmed emails, then creates the users with temporary passwords and
their single nominated group. The default `human_enrollment = null` creates no
users; reverting an enrolled configuration to null proposes their removal and
must be reviewed as such. Changing an alias, role, email or password also needs
a fresh reviewed plan. Reusing ciphertext under another role/pool/alias fails
decryption. There is no automatic resealing or account-recovery script.

Private Terraform state and saved plans contain decrypted identity data and
temporary credentials. Keep the existing encrypted state/plan custody and
value-free GitHub review artifacts. The `human_enrollment` output contains only opaque alias,
observed Cognito subject ID, nominated role and status, never email/password.
Resource/action summaries use the opaque alias rather than the email.
The campaign returns this under `application_deployment.human_enrollment`, with
its same application identity references in `application_deployment.identity_preparation`.

After the authenticated saved-plan apply succeeds, `enrollment_receipt.py` retains
an identity-only carrier in the existing private plan bucket. It binds the actual
Cognito subjects and reviewed roles to the candidate, application task revisions,
configuration, original plan version, original review artifact and apply attempt.
The exact original value-free `review.json` is retained alongside it under
`hosted-pilot/plans/<run>/<plan-attempt>/`. Each apply attempt uses its own
`human-identities-<apply-attempt>.json`; conditional writes and exact readback
preserve earlier objects. A lost write reply permits readback, never a blind
mutation retry. An unresolved result remains a failed apply job.

Only `human-enrollment-reference.json` is published as a GitHub artifact. The
carrier and full Terraform outputs stay private. The reference names an immutable
version, digest and original custody key; it contains no user identities. A null
enrollment produces no carrier. Missing task revisions, mismatched subjects,
changed roles, a foreign target or a failed Terraform apply cannot produce an
accepted enrollment receipt. The native membership helper must independently
authenticate the successful original workflow job and review before using it.
Provisioning always records `qualification: false`; neither this receipt nor a
successful deployment proves that a human logged in or approved anything.

## Native Server membership

The application provisions an independent SCIM bearer secret and binds its exact
version and actual Cognito issuer into the Server task. Its
`native_membership_credentials` output contains only the SCIM and root secret
ARNs and immutable version IDs. In a campaign this output is under
`application_deployment`; neither output contains a credential value.

Before the first native SQL/key bootstrap, review `native_bootstrap.humanMemberships`
into the campaign configuration. It has exactly three fields:

- `schemaVersion: 1`.
- `enrollment`: the complete original `human-enrollment-reference.json` object.
- `credentials`: the actual application's `native_membership_credentials` object.

The foundation must include its distinct native-bootstrap task boundary before
applying this campaign configuration. Both steps use their checked saved-plan
workflows. The campaign refuses a different application's credential versions,
a foreign enrollment bucket, or a saved-plan binary substituted for the identity
carrier. Only the private bootstrap task receives the version-pinned SCIM/root
credentials. Its execution role injects them; its task role cannot read secrets.
Controller and worker custody-key exclusions remain unchanged.

Review the resulting `native_bootstrap_handoff` into
`certification/agent-e2e/native-bootstrap-handoff.json`, preserving the original
operation and accounting references. Dispatch `hosted-pilot-native-bootstrap.yml`
only from the reviewed protected source. The task independently verifies the
original successful enrollment apply, configuration, private object versions and
current deployed Server before using actual SCIM/role APIs. It uses observed raw
Cognito subjects, preserves separate author/approver roles, and checks the exact
effective grants. It does not invent identities, write membership tables directly,
or issue human login tokens.

Private enrollment and review objects use object-level S3 KMS encryption context;
bucket keys are disabled and readback refuses them. The dedicated task boundary
allows only these JSON object forms through S3, and the task's identity policy
further limits the original keys and enrollment version. Foundation state,
budget records and saved-plan binaries remain outside that read authority.

Membership mutations retain intent before sending. A lost response permits native
readback without another POST/PUT. Membership reconciliation runs before the first
parent SQL/key intent; an already-started parent bootstrap refuses all new effects.
Keep unresolved tasks, reservations and observations for explicit recovery. A
completed membership receipt remains private, with only its digest and enrollment
reference included in the parent receipt; it still records `qualification: false`.

## First login and qualification

After successful apply, the operator securely hands each person their own
temporary credential from the private handoff. Automatic email invitations are
suppressed. Cognito requires a password change at first login; this pool's
temporary-password validity is one day. An expired credential requires a new
sealed value and the reviewed saved-plan path. Retained old ciphertext is not
a credential-renewal mechanism.

Each person signs in through the real hosted Console URL. Record the actual
Cognito issuer and `sub`, then the independently observed Server/Console
identity. Do not parse a namespaced actor/Studio owner ID to invent a Cognito
subject. Use those actual identities in the separate application-grant path.
Ordinary authors require the narrow current-membership grants tracked in
[Server #6533](https://github.com/honua-io/honua-server/issues/6533); Cognito group
membership alone does not grant native workspace admission. Do not add authors
to administrator roles to bypass that prerequisite.
An `email_verified` attribute here records the operator's enrollment attestation;
it is not proof that a person signed in or approved anything.

The demo qualification still requires separate-principal approval, author
refusal for admin-only actions, correct audience/tenant access, live AI journeys,
failure recovery, five real user sessions and an installed-candidate recording.
Offline provider mocks and sealing-helper tests cannot substitute for any of
those observations.
