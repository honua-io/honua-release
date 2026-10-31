"""Keep copyable callers aligned with the reusable workflows they invoke."""

from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs/examples/vendor-guard"


def load_yaml(path: Path) -> dict:
    # BaseLoader keeps the GitHub key `on` as a string under YAML 1.1 parsers.
    with path.open(encoding="utf-8") as stream:
        return yaml.load(stream, Loader=yaml.BaseLoader)


class VendorGuardCallerExamplesTests(unittest.TestCase):
    def test_event_caller_matches_triggers_and_scopes_write_permissions_per_surface(self) -> None:
        caller = load_yaml(EXAMPLES / "event-caller.yml.example")
        called = [
            load_yaml(ROOT / ".github/workflows/guard-vendor-event.yml"),
            load_yaml(ROOT / ".github/workflows/guard-vendor-commit-comment.yml"),
        ]
        triggers = {key: value for workflow in called for key, value in workflow["on"].items()
                    if key != "workflow_call"}
        self.assertEqual(caller["on"], triggers)
        self.assertEqual(caller["permissions"], {})
        self.assertEqual(caller["jobs"]["remediate-text"]["permissions"], {
            "contents": "read", "issues": "write", "pull-requests": "write",
        })
        self.assertEqual(caller["jobs"]["remediate-commit-comment"]["permissions"], {
            "contents": "write",
        })

    def test_audit_caller_exposes_shard_recovery_with_read_only_permissions(self) -> None:
        caller = load_yaml(EXAMPLES / "audit-caller.yml.example")
        called = load_yaml(ROOT / ".github/workflows/audit-vendor-github.yml")
        self.assertEqual(caller["on"]["schedule"], called["on"]["schedule"])
        self.assertEqual(caller["on"]["workflow_dispatch"], called["on"]["workflow_dispatch"])
        self.assertEqual(caller["permissions"], {"contents": "read", "issues": "read", "pull-requests": "read"})
        self.assertEqual(caller["jobs"]["audit"]["with"]["review_shard"], "${{ inputs.review_shard || '' }}")

    def test_callers_pin_the_same_immutable_release_commit(self) -> None:
        refs = []
        for file, job, workflow in (
            ("event-caller.yml.example", "remediate-text", "guard-vendor-event.yml"),
            ("event-caller.yml.example", "remediate-commit-comment", "guard-vendor-commit-comment.yml"),
            ("audit-caller.yml.example", "audit", "audit-vendor-github.yml"),
        ):
            uses = load_yaml(EXAMPLES / file)["jobs"][job]["uses"]
            prefix = f"honua-io/honua-release/.github/workflows/{workflow}@"
            self.assertTrue(uses.startswith(prefix))
            ref = uses.removeprefix(prefix)
            self.assertRegex(ref, re.compile(r"^[0-9a-f]{40}$"))
            refs.append(ref)
        self.assertEqual(refs[0], refs[1])


if __name__ == "__main__":
    unittest.main()
