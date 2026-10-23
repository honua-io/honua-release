# Release-cell Lambda version inventory

This root adopts only the existing `honua-release-cicd:release-cicd-guardrails`
inline policy. Its single change exempts `lambda:ListVersionsByFunction` from
the standing-resource deny. All other statements, resource conditions, regions,
mutation restrictions and existing grants are preserved. It adds no role,
role chaining, write permission or secret-value API. The existing release role's
grants must independently allow the inventory action.

The complete-cell retirement helper needs every regional function version to
check whether a foreign function consumes a cell's retained runtime settings.
The read must succeed before a delete-only retirement plan can proceed.
Version inventory includes environment configuration; the runner must keep it
private and must never publish the values as evidence.

The deployment entry point is the checked-in
`honua-iac/.github/workflows/release-cell-inventory-policy.yml` on protected trunk.
Pass the full reviewed release trunk commit and `mode=plan`. After reviewing its
metadata receipt, use `mode=deploy` through the existing source-defined
`terraform-live-approval` human-review environment. The executor imports the
fixed resource, reviews one in-place saved plan, rechecks live policy drift,
applies that exact plan, and verifies live readback. It refuses any other live
policy revision, creation, replacement, deletion or second resource.

The policy is already a standing resource with an immutable import identity;
each ephemeral runner imports that same identity. Terraform state and full
plans remain private to that job, and only identity, hashes and status leave it.
After an interrupted apply, a new run accepts either the exact original policy
or the exact desired policy; any third state requires source reconciliation.
There is no policy deletion or rollback-to-broad-access command.

A successful policy deployment is permission preparation only. The original
cell recovery workflows must still produce actual complete inventories, saved
delete-only plans, deletion observations and unresolved-cost reconciliation.
