"""Owner-approved patch policy; authenticated inputs are retained by the existing fetcher."""
from __future__ import annotations

from datetime import datetime
import re

# Reuse is conservative: whole-platform/security/journey/rollback evidence is never reused.
# These certificate classes cover the named runtime/client identities plus all shared fixtures.
REUSE_COMPONENTS = {
    "cite": frozenset({"honua-server"}),
    "esri-bundle": frozenset({"honua-server", "honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python"}),
}


def profile(record, selected, evidence_dir):
    from check_promotion_readiness import ReadinessError, _digest, _load, _time
    from fetch_promotion_evidence import _identity, MINTING_WORKFLOWS, MINTING_EVENTS, FetchError
    name = selected.get("profile", "ga")
    if name not in ("ga", "patch") or record.get("profile", "ga") != name:
        raise ReadinessError("promotion profile must match the signed selected lock")
    if name == "ga":
        if "baseline" in record or "changedComponents" in selected or "baselineLockDigest" in selected:
            raise ReadinessError("patch declarations require the signed patch profile")
        return {"name": name, "hours": 48, "canaries": 7}
    label = record.get("platformLabel")
    if (not isinstance(label, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[1-9][0-9]*-rc\.[1-9][0-9]*", label) is None
            or selected.get("platform", {}).get("id") != "honua-" + label):
        raise ReadinessError("patch profile requires a matching signed nonzero patch RC label")
    baseline = record.get("baseline")
    if not isinstance(baseline, dict):
        raise ReadinessError("patch requires an authenticated baseline")
    from check_promotion_readiness import _run_id
    run = _run_id(baseline.get("runId"), "baseline runId")
    root = evidence_dir / "baselines" / run
    previous = _load(root / "platform-lock.json", "patch baseline lock")
    metadata = _load(root / "run.json", "patch baseline metadata")
    try:
        _identity(metadata, "honua-io/honua-release", "trunk", run, MINTING_WORKFLOWS, MINTING_EVENTS)
    except FetchError as exc:
        raise ReadinessError("patch baseline producer identity refused") from exc
    train = _load(evidence_dir / "trains" / str(record.get("rcTrainRunId")) / "run.json", "selected train metadata")
    if _time(metadata.get("updated_at"), "baseline completedAt") >= _time(train.get("updated_at"), "selected train completedAt"):
        raise ReadinessError("patch baseline must precede the selected minting train")
    publication = _load(root / "publication.json", "published baseline release")
    tag = baseline.get("releaseTag")
    published = re.fullmatch(r"honua-([0-9]+)\.([0-9]+)\.([0-9]+)", tag or "")
    patch = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([1-9][0-9]*)-rc\.[1-9][0-9]*", label)
    baseline_platform = previous.get("platform", {}).get("id", "")
    baseline_version = re.fullmatch(r"honua-([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?:-rc\.[1-9][0-9]*)?", baseline_platform)
    if (not published or not patch or not baseline_version
            or published.group(1, 2) != patch.group(1, 2)
            or int(published[3]) >= int(patch[3])
            or baseline_version.group(1, 2) != published.group(1, 2)
            or int(baseline_version[3] or 0) != int(published[3])
            or publication.get("tag_name") != tag or publication.get("draft") is not False
            or publication.get("prerelease") is not False
            or type(publication.get("id")) is not int or publication["id"] <= 0
            or publication.get("html_url") != "https://github.com/honua-io/honua-release/releases/tag/" + tag
            or _time(publication.get("published_at"), "baseline publishedAt") > _time(train.get("updated_at"), "selected train completedAt")):
        raise ReadinessError("patch baseline must be an actual published same-series lower patch release")
    digest = _digest(root / "platform-lock.json")
    if _digest(root / "published-platform-lock.json") != digest:
        raise ReadinessError("published baseline lock differs from the certified baseline bytes")
    if digest != baseline.get("lockDigest") or digest != selected.get("baselineLockDigest"):
        raise ReadinessError("patch baseline digest does not match authenticated retained bytes")
    before, after = previous.get("components"), selected.get("components")
    changed = selected.get("changedComponents")
    if (not isinstance(before, dict) or not before or not isinstance(after, dict) or not after
            or not isinstance(changed, list) or not changed or any(not isinstance(c, str) for c in changed)
            or len(changed) != len(set(changed))):
        raise ReadinessError("patch requires exact nonempty changedComponents")
    actual = {c for c in set(before) | set(after) if before.get(c) != after.get(c)}
    if set(changed) != actual:
        raise ReadinessError("changedComponents does not match the complete baseline component diff")
    return {"name": name, "hours": 12, "canaries": 2, "baseline": previous,
            "selected": selected, "baselineDigest": digest, "changed": actual,
            "baselineRoot": root, "selectedRoot": evidence_dir / "trains" / str(record["rcTrainRunId"])}


def reused_receipt(name, row, receipt, metadata, policy, now: datetime):
    """No new freshness window and no caller-selected narrow component coverage."""
    if "reusedComponents" not in row:
        return None
    from check_promotion_readiness import ReadinessError, _time
    covered = REUSE_COMPONENTS.get(name)
    declared = row.get("reusedComponents")
    if (policy["name"] != "patch" or covered is None or not isinstance(declared, list)
            or len(declared) != len(set(declared)) or set(declared) != covered
            or covered & policy["changed"]):
        raise ReadinessError("class or changed component is ineligible for evidence reuse")
    from fetch_promotion_evidence import _identity, RECEIPT_PRODUCERS, FetchError
    if name not in RECEIPT_PRODUCERS:
        raise ReadinessError("reused evidence has no allowlisted producer")
    try:
        _identity(metadata, "honua-io/honua-release", "trunk", str(row["runId"]), *RECEIPT_PRODUCERS[name])
    except FetchError as exc:
        raise ReadinessError("reused evidence producer identity refused") from exc
    before, after = policy["baseline"], policy["selected"]
    for key in ("fixtures", "contentDigests"):
        if key not in before or before[key] != after.get(key):
            raise ReadinessError("reused evidence shared fixture/content identity changed or missing")
    identities = receipt.get("componentIdentities")
    if not isinstance(identities, dict) or set(identities) != covered:
        raise ReadinessError("reused receipt must retain every policy-covered component identity")
    import hashlib
    import yaml
    manifests = []
    for lock, root in ((before, policy["baselineRoot"]), (after, policy["selectedRoot"])):
        try:
            payload = (root / "platform-manifest.yaml").read_bytes()
            binding = lock["sourceInputs"]["platformManifest"]["sha256"]
            if "sha256:" + hashlib.sha256(payload).hexdigest() != binding:
                raise ReadinessError("reused evidence manifest bytes are not bound by the source lock")
            manifest = yaml.safe_load(payload)
            manifests.append(manifest["components"])
        except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
            raise ReadinessError("reused evidence requires retained hash-bound component manifests") from exc
    for component in covered:
        identity = before["components"].get(component)
        manifest_identity = manifests[0].get(component)
        if (not manifest_identity or manifest_identity != manifests[1].get(component)
                or not identity or identity != after["components"].get(component) or identity != identities[component]):
            raise ReadinessError("reused component/artifact identity differs")
    if row.get("lockDigest") != policy["baselineDigest"] or receipt.get("lockDigest") != policy["baselineDigest"]:
        raise ReadinessError("reused receipt does not bind the authenticated baseline lock")
    if not _time(receipt.get("completedAt"), "reused completedAt") <= now <= _time(receipt.get("freshUntil"), "reused freshUntil"):
        raise ReadinessError("reused evidence is stale or future-dated")
    return policy["baselineDigest"]
