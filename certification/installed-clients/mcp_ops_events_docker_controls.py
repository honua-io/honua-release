"""Real Docker safe option projections, not a Honua runtime/candidate proof."""
import os
import re
import subprocess
import unittest

import mcp_ops_events_config as configuration
from mcp_ops_events_config_controls import selected
from mcp_render_validation_controls import runtime


class LiveConfigurationProjectionControls(unittest.TestCase):
    def setUp(self):
        service = os.environ.get("CONTROL_REDIS_CONTAINER", "")
        if os.environ.get("GITHUB_ACTIONS") != "true" or not re.fullmatch(r"[a-f0-9]{64}", service):
            raise ValueError("configuration controls require the workflow-owned Docker service")
        self.image = self.command(["docker", "inspect", "--format", "{{.Image}}", service]).strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image):
            raise ValueError("owned control image was not immutable")
        self.containers = []

    def command(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("owned Docker option control refused")
        return result.stdout

    def container(self, extra=None):
        declaration = selected()
        args = ["docker", "create", "--label", "honua.certification.source-control=ops-events", "--entrypoint", "/bin/sh"]
        for key, value in {**declaration["environment"], "PRIVATE_CONTROL_KEY": "controlled-secret-never-project", **(extra or {})}.items():
            args.extend(["--env", key + "=" + value])
        args.extend([self.image, "-c", "sleep 120"])
        container = self.command(args).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container):
            raise ValueError("owned Docker allocation was not exact")
        self.containers.append(container)
        self.command(["docker", "start", container])
        binding = runtime()
        # Explicit projection-only binding; this Redis utility image is not
        # represented as an observed Honua server or an eligible candidate.
        binding.update(containerId=container, candidateSourceSha=declaration["candidateSourceSha"],
            candidateImage=declaration["candidateImage"])
        return declaration, binding

    def tearDown(self):
        for container in self.containers:
            label = self.command(["docker", "inspect", "--format", '{{index .Config.Labels "honua.certification.source-control"}}', container]).strip()
            if label != "ops-events":
                raise ValueError("control cleanup lost its owned container label")
            self.command(["docker", "rm", "-f", container])

    def test_real_daemon_projects_only_the_declared_safe_values_and_counts_full_scope(self):
        declaration, binding = self.container()
        observed = configuration.observe_configuration(declaration, binding)
        self.assertEqual(observed["environment"], declaration["environment"])
        self.assertEqual(observed["selectedScopeEntryCount"], len(configuration.SELECTED_KEYS))
        self.assertFalse(observed["deployedTargetObserved"] or observed["actuationRequested"])
        raw = self.command(["docker", "inspect", "--format", configuration.option_template(), binding["containerId"]])
        self.assertNotIn("controlled-secret-never-project", raw)
        self.assertNotIn("PRIVATE_CONTROL_KEY", raw)
        lines = raw.splitlines()
        if lines and lines[-1] == "":
            lines.pop()
        self.assertEqual(len(lines), 2 * len(configuration.SELECTED_KEYS))

    def test_extra_or_case_aliased_options_are_refused_without_projecting_values(self):
        for key in ("ControlPlane__DeployTargets__2__ArtifactReference", "controlplane__platformrelease__version"):
            with self.subTest(key=key):
                declaration, binding = self.container({key: "controlled-aliased-secret-never-project"})
                raw = self.command(["docker", "inspect", "--format", configuration.option_template(), binding["containerId"]])
                self.assertNotIn("controlled-aliased-secret-never-project", raw)
                with self.assertRaisesRegex(ValueError, "option values/coverage differ"):
                    configuration.observe_configuration(declaration, binding)


if __name__ == "__main__":
    unittest.main(verbosity=2)
