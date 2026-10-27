"""The G3 dispatch runs the public quickstart fences and writes a receipt."""

from pathlib import Path

import pytest

import g3_compose_quickstart as g3

SHA = "a" * 40
DOCUMENTED = "ghcr.io/honua-io/honua-server:2026.1-rc"
SECRET = "supersecretvalue"

PAGE = """\
Put this in it as `compose.yaml`:

<!-- docs-validation:install.compose mode=run -->
```yaml
services:
  honua:
    image: ${HONUA_IMAGE:?Set the server image}
    platform: linux/amd64
```

<!-- docs-validation:install.secrets mode=run -->
```bash
umask 077
pass=supersecretvalue
cat > .env <<EOF
HONUA_IMAGE=ghcr.io/honua-io/honua-server:2026.1-rc
POSTGRES_PASSWORD=${pass}
EOF
export G3_KEEP=from-secrets
echo "leak ${pass}"
# g3-fixture-tail
```

<!-- docs-validation:install.windows-secrets mode=skip reason="Native Windows alternative" -->
```powershell
Write-Output skipped
```

<!-- docs-validation:install.clients mode=run -->
```bash
export G3_KEEP=from-clients
```

Save this as `quickstart.py`:

<!-- docs-validation:install.publish mode=run -->
```python
print("placeholder")
```

<!-- docs-validation:install.query mode=run -->
```bash
test -f quickstart.py
echo "$G3_KEEP" > kept.txt
echo west
echo east
```

<!-- docs-validation:install.cleanup mode=skip reason="Harness tears down its isolated project on every exit" -->
```bash
docker compose down --volumes    # stop and permanently delete this project's data
```
"""

FAILURE_PAGE = PAGE.replace("# g3-fixture-tail\n", "false\n", 1)


def _runner(calls: list[list[str]]):
    def run(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        calls.append(list(argv))
        if argv[:4] == ["docker", "compose", "down", "--volumes"]:
            return 0, "removed"
        if argv[0] == "bash":
            return g3.default_runner(argv, cwd, timeout)
        raise AssertionError(argv)

    return run


def _run(tmp_path: Path, page: str, image: str = "2026.1-rc"):
    run_dir = tmp_path / "run"
    log_dir = tmp_path / "log"
    actions, documented, requested, overridden = g3.plan_fences(page, image)
    calls: list[list[str]] = []
    execution = g3.execute(actions, run_dir, log_dir, runner=_runner(calls))
    passed, gate, failure = g3.conclude(execution.steps, overridden)
    return actions, documented, requested, overridden, execution, passed, gate, failure, calls, run_dir, log_dir


def test_documented_tag_preserves_the_secrets_fence_and_skips_windows_and_cleanup(tmp_path: Path):
    actions, documented, requested, overridden, execution, passed, gate, failure, calls, run_dir, log_dir = _run(
        tmp_path, PAGE
    )

    assert documented == requested == DOCUMENTED
    assert overridden is False
    secrets = next(action for action in actions if action.block_id == "install.secrets")
    assert "HONUA_IMAGE=ghcr.io/honua-io/honua-server:2026.1-rc" in secrets.body
    script = (log_dir / "quickstart.sh").read_text(encoding="utf-8")
    assert secrets.body in script
    assert "Write-Output skipped" not in script
    assert "stop and permanently delete" not in script
    assert calls[-1][:4] == ["docker", "compose", "down", "--volumes"]
    assert passed and gate and failure == ""
    assert (run_dir / "kept.txt").read_text(encoding="utf-8").strip() == "from-clients"
    assert "${HONUA_IMAGE:?Set the server image}" in (run_dir / "compose.yaml").read_text(encoding="utf-8")
    assert "platform: linux/amd64" in (run_dir / "compose.yaml").read_text(encoding="utf-8")
    assert SECRET not in (log_dir / "quickstart.log").read_text(encoding="utf-8")
    assert "[redacted]" in (log_dir / "quickstart.log").read_text(encoding="utf-8")


def test_image_override_replaces_only_the_honua_image_line(tmp_path: Path):
    actions, documented, requested, overridden, execution, passed, gate, _, _, _, log_dir = _run(
        tmp_path, PAGE, "ghcr.io/honua-io/honua-server:2026.1"
    )

    assert documented == DOCUMENTED
    assert requested == "ghcr.io/honua-io/honua-server:2026.1"
    assert overridden is True
    script = (log_dir / "quickstart.sh").read_text(encoding="utf-8")
    assert "HONUA_IMAGE=ghcr.io/honua-io/honua-server:2026.1" in script
    assert "HONUA_IMAGE=ghcr.io/honua-io/honua-server:2026.1-rc" not in script
    assert "${HONUA_IMAGE:?Set the server image}" in script
    assert passed is True and gate is False
    compose = next(action.body for action in actions if action.block_id == "install.compose")
    assert "2026.1\n" not in compose


def test_a_failed_step_stops_the_later_fences_and_redacts_the_excerpt(tmp_path: Path):
    _, _, _, overridden, execution, passed, gate, failure, calls, run_dir, _ = _run(tmp_path, FAILURE_PAGE)

    by_id = {step.block_id: step for step in execution.steps}
    assert by_id["install.secrets"].status == "failed"
    assert by_id["install.query"].status == "not_run"
    assert not (run_dir / "quickstart.py").exists()
    assert passed is False and gate is False and overridden is False
    assert "does not turn G3 green" not in failure
    assert SECRET not in by_id["install.secrets"].output
    assert "[redacted]" in by_id["install.secrets"].output
    assert calls[-1][:4] == ["docker", "compose", "down", "--volumes"]


def test_query_without_the_documented_features_is_not_a_pass(tmp_path: Path):
    page = PAGE.replace("echo west\necho east\n", "echo nothing\n", 1)
    _, _, _, _, execution, passed, gate, failure, _, _, _ = _run(tmp_path, page)

    assert passed is False and gate is False
    assert "west and east" in failure
    query = next(step for step in execution.steps if step.block_id == "install.query")
    assert query.status == "failed"


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/evil/honua-server:2026.1-rc",
        "2026.1-rc;touch /tmp/x",
        "ghcr.io/honua-io/honua-server:2026.1-rc extra",
        "sha256:" + "a" * 64,
        "ghcr.io/honua-io/honua-server@sha256:" + "A" * 64,
    ],
)
def test_rejects_image_references_that_are_not_the_public_server(image: str):
    with pytest.raises(g3.QuickstartError):
        g3.normalize_image(image, DOCUMENTED)


def test_digest_override_is_explicit_and_not_the_channel_tag():
    digest = "ghcr.io/honua-io/honua-server@sha256:" + "ab" * 32
    actions, documented, requested, overridden = g3.plan_fences(PAGE, digest)

    assert documented == DOCUMENTED
    assert requested == digest
    assert overridden is True
    secrets = next(action.body for action in actions if action.block_id == "install.secrets")
    assert f"HONUA_IMAGE={digest}" in secrets
    assert DOCUMENTED not in secrets


def test_powershell_run_fence_is_refused():
    page = "<!-- docs-validation:install.windows-secrets mode=run -->\n```powershell\nGet-ChildItem\n```\n"
    with pytest.raises(g3.QuickstartError, match="cannot run powershell"):
        g3.plan_fences(page + _image_fence(), "")


def _image_fence() -> str:
    return (
        "<!-- docs-validation:install.secrets mode=run -->\n"
        "```bash\n"
        f"HONUA_IMAGE={DOCUMENTED}\n"
        "```\n"
        "Save this as `quickstart.py`:\n"
        "<!-- docs-validation:install.publish mode=run -->\n"
        "```python\nprint('x')\n```\n"
        "<!-- docs-validation:install.query mode=run -->\n"
        "```bash\nprintf '%s\\n' west east\n```\n"
    )


def test_unannotated_fence_is_refused():
    with pytest.raises(g3.QuickstartError, match="no docs-validation annotation"):
        g3.parse_document("```bash\necho hi\n```\n")


def test_run_directory_must_be_empty_and_outside_the_log(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "already").write_text("x", encoding="utf-8")
    with pytest.raises(g3.QuickstartError, match="start empty"):
        g3.execute([], run_dir, tmp_path / "log", teardown=False)
    assert (run_dir / "already").read_text(encoding="utf-8") == "x"

    nested = tmp_path / "outer"
    with pytest.raises(g3.QuickstartError, match="outside the quickstart"):
        g3.execute([], nested, nested / "log", teardown=False)


def test_relative_log_directory_resolves_the_script_before_changing_directories(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    actions, _, _, _ = g3.plan_fences(PAGE, "2026.1-rc")
    argvs: list[list[str]] = []

    def run(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        argvs.append(list(argv))
        if argv[0] == "bash":
            return g3.default_runner(argv, cwd, timeout)
        return 0, ""

    g3.execute(actions, Path("run"), Path("logs"), runner=run, teardown=False)

    script = next(argv[-1] for argv in argvs if argv[0] == "bash")
    assert Path(script).is_absolute()
    assert Path(script).is_file()
    assert "No such file" not in (tmp_path / "logs" / "quickstart.log").read_text(encoding="utf-8")


def test_receipt_names_the_gate_without_closing_it_on_failure(tmp_path: Path):
    _, _, _, _, execution, passed, gate, failure, _, _, _ = _run(tmp_path, FAILURE_PAGE)
    text = g3.render_receipt(
        started="2026-10-27T02:46:01Z",
        finished="2026-10-27T02:46:09Z",
        runner="GitHub-hosted ubuntu-latest",
        run_id="101",
        run_attempt="1",
        os_text="Linux",
        arch="x86_64",
        docker="29.8.0",
        compose="Docker Compose version v2.29.7",
        python="Python 3.12.3",
        openssl="OpenSSL 3.0.13",
        source_sha=SHA,
        documented_image=DOCUMENTED,
        image=DOCUMENTED,
        overridden=False,
        passed=passed,
        gate_green=gate,
        failure=failure,
        steps=execution.steps,
        registry_note="HTTP 404 MANIFEST_UNKNOWN",
        digest="",
        teardown="exit 0",
    )

    assert text.startswith("# G3 clean-machine quickstart run — 2026-10-27")
    assert "**Failed.**" in text
    assert "does not turn G3 green" in text
    assert "pre-cut rehearsal digest" in text
    assert "HTTP 404 MANIFEST_UNKNOWN" in text
    assert SECRET not in text
    assert "[redacted]" in text
    assert "honua-release was checked out in another directory" in text
    body = g3.render_pr_body(
        run_id="101",
        source_sha=SHA,
        image=DOCUMENTED,
        passed=False,
        gate_green=False,
        receipt="docs/launch-gates/G3-clean-machine-quickstart-2026-10-27-run-101-1.md",
    )
    assert "Refs #1009" in body
    assert "Closes #" not in body
    assert "Co-Authored-By" not in body
    assert "Generated with" not in body


def test_passing_documented_receipt_is_the_g3_evidence():
    text = g3.render_receipt(
        started="2026-10-27T02:46:01Z",
        finished="2026-10-27T02:55:01Z",
        runner="GitHub-hosted ubuntu-latest",
        run_id="101",
        run_attempt="1",
        os_text="Linux",
        arch="x86_64",
        docker="29",
        compose="v2.29.7",
        python="Python 3.12.3",
        openssl="OpenSSL 3",
        source_sha=SHA,
        documented_image=DOCUMENTED,
        image=DOCUMENTED,
        overridden=False,
        passed=True,
        gate_green=True,
        failure="",
        steps=[],
        registry_note="",
        digest="ghcr.io/honua-io/honua-server@sha256:" + "ab" * 32,
        teardown="exit 0",
    )

    assert "This record is the G3 evidence" in text
    assert "does not turn G3 green" not in text
    assert "first-map page was not part of this dispatch" in text
    assert g3.pr_title(True, True, "101") == "docs: record the G3 compose quickstart run 101"


def test_compose_older_than_the_quickstart_blocks_before_a_run():
    assert g3.host_preflight("Docker Compose version v2.20.0") == (
        "Docker Compose 2.20.0 is older than the quickstart's 2.23.1."
    )
    assert g3.host_preflight("Docker Compose version v2.23.1") is None
    assert g3.host_preflight("unavailable") is None


def test_offline_main_commits_a_receipt_and_removes_the_run_directory(tmp_path: Path, monkeypatch):
    page = tmp_path / "quickstart.md"
    page.write_text(PAGE, encoding="utf-8")
    receipt_dir = tmp_path / "docs" / "launch-gates"
    log_dir = tmp_path / "logs"
    run_dir = tmp_path / "run"
    monkeypatch.chdir(tmp_path)
    code = g3.main(
        [
            "--quickstart",
            str(page),
            "--image",
            "2026.1-rc",
            "--run-dir",
            str(run_dir),
            "--log-dir",
            str(log_dir),
            "--receipt-dir",
            "docs/launch-gates",
            "--source-sha",
            SHA,
            "--runner",
            "GitHub-hosted ubuntu-latest",
            "--run-id",
            "55",
            "--run-attempt",
            "1",
            "--offline",
        ]
    )

    assert code == 0
    assert not run_dir.exists()
    receipts = list(receipt_dir.glob("G3-clean-machine-quickstart-*-run-55-1.md"))
    assert len(receipts) == 1
    text = receipts[0].read_text(encoding="utf-8")
    assert "G3 evidence" in text
    assert SECRET not in text
    assert SECRET not in (log_dir / "quickstart.log").read_text(encoding="utf-8")
    outcome = (log_dir / "outcome.json").read_text(encoding="utf-8")
    assert '"gate_green": true' in outcome
    body = (log_dir / "pr-body.md").read_text(encoding="utf-8")
    assert "Refs #1009" in body
    assert "Closes #" not in body
    assert not list(log_dir.rglob(".env"))


def test_workflow_is_a_trunk_dispatch_that_opens_a_receipt_pr():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/g3-compose-quickstart.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "runs-on: ubuntu-latest" in text
    assert 'default: "2026.1-rc"' in text
    assert "refs/heads/trunk" in text
    assert "honua-io/honua-release" in text
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1" in text
    assert "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2" in text
    assert "RELEASE_GH_TOKEN" in text
    assert 'IMAGE: ${{ inputs.image }}' in text
    assert text.count("${{ inputs.image }}") == 1
    assert "--offline" not in text
    assert "first-map.md" not in text
    assert "069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a" not in text
    assert "git push origin HEAD:trunk" not in text
    assert 'git push origin "HEAD:refs/heads/${branch}"' in text
    assert "docs/get-started/quickstart.md" in text
    assert "cancel-in-progress: false" in text
    assert "contents: write" not in text
    for title in (
        "docs: record the G3 compose quickstart run",
        "docs: record the failed G3 compose quickstart run",
        "docs: record a G3 quickstart run against an override image",
    ):
        assert title in text


def test_live_public_quickstart_when_the_server_checkout_is_present(tmp_path: Path):
    page = Path(__file__).resolve().parents[2] / "honua-server/docs/get-started/quickstart.md"
    if not page.is_file():
        pytest.skip("honua-server quickstart is not checked out beside this repo")
    text = page.read_text(encoding="utf-8")
    actions, documented, requested, overridden = g3.plan_fences(text, "2026.1-rc")

    assert documented == requested == DOCUMENTED
    assert overridden is False
    by_id = {action.block_id: action for action in actions}
    assert by_id["install.compose"].filename == "compose.yaml"
    assert by_id["install.publish"].filename == "quickstart.py"
    assert by_id["install.windows-secrets"].kind == "skip"
    assert by_id["install.cleanup"].kind == "skip"
    assert "python quickstart.py" in by_id["install.query"].body
    script = g3.render_script(actions)
    assert by_id["install.secrets"].body in script
    assert "stop and permanently delete" not in script
    calls: list[list[str]] = []
    g3.execute(
        [by_id["install.compose"]],
        tmp_path / "run",
        tmp_path / "log",
        runner=_runner(calls),
        teardown=False,
    )
    written = (tmp_path / "run" / "compose.yaml").read_text(encoding="utf-8")
    assert "${HONUA_IMAGE:?Set the server image}" in written
    assert "platform: linux/amd64" in written
    assert "docker" not in {call[0] for call in calls}
