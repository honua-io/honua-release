import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inject_upgrade_failure as injector  # noqa: E402


def test_injector_preserves_candidate_but_forces_readiness_failure():
    manifest = {
        "kind": "Deployment",
        "metadata": {"labels": {"app.kubernetes.io/instance": "honua"}},
        "spec": {"template": {"spec": {"containers": [{"name": "server", "image": "candidate@sha256:abc"}]}}},
    }
    result = injector.inject([manifest])[0]
    container = result["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "candidate@sha256:abc"
    assert container["readinessProbe"]["httpGet"]["port"] == 1


def test_injector_refuses_a_chart_without_the_target_deployment():
    with pytest.raises(SystemExit, match="no honua Deployment"):
        injector.inject([{"kind": "Service", "metadata": {}}])
