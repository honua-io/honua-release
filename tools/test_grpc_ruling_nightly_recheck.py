"""The gRPC scope ruling is re-checked by hand before it carries to a server whose surface changed."""
import json

import convergence_rebind as MODULE


def test_grpc_ruling_is_rechecked_at_the_2026_11_21_nightly_candidate_server():
    """The nightly candidate a997a37 changes FeatureService sort-field resolution only, so the
    surface hash differs from de512e8's and the ruling was re-checked before it carries."""
    document = json.loads((MODULE.ROOT / MODULE.GRPC_OPERATIONS).read_text(encoding="utf-8"))
    ruling = next(r for r in document["rulings"] if r["id"] == MODULE.GRPC_RULING)
    assert ruling["verified_server_commits"][-2:] == [
        "de512e8123bdfbfbe20f692b236e37853b4f8294", "a997a37e6953d19a4db483aa9160a113f06ae5ef"]
    recheck = ruling["evidence"].split("Re-checked at a997a37", 1)[1]
    assert "No RPC was added or removed" in recheck
    assert "still returns UNIMPLEMENTED for ExecutePlan and ExecutePlanStream" in recheck
