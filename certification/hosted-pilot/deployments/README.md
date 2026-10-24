`foss4g-image-preparation.tfvars.json` is the source identity used only to fund
candidate-independent controller or worker image publication. It contains the
chosen account's region, isolated prefix, owner and cleanup deadline. It is not a
complete Terraform workload deployment: the root still requires actual candidate
artifacts and operator ingress before a saved workload plan can be created.

The reviewed IaC foundation configuration uses `image_preparation_funding=true`
and null budget source/config hashes. The checked bootstrap resolves those two
fields exclusively from this fixed tracked file at its actual, verified release
workflow SHA. It rejects prefilled replacements, changed bytes or another
region/prefix/owner/deadline. The helper-only flag never reaches Terraform.
The deployed private owner ledger therefore contains actual source identities,
not placeholders. A single immutable ledger reservation funds one producing run.
Reconcile original costs and publish another reviewed owner version before a
different publication or deployment; do not reuse or reset the first allowance.

The first source-resolved allowance has the stable original
`reservation_id=foundation-image-preparation-001`. Changing source hashes or
creating another ledger version cannot create a second claim under that identity.
A subsequent publication requires a reviewed new allocation ID and reconciliation
of the prior allocation into its nonzero reserved or genuinely settled history.
