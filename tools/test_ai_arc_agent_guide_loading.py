"""Release-session decision 2026-11-02 (under founder D1): each arm's CLI loads the package's shipped AGENTS.md as
project instructions, as it would in the install directory, and no other project or user instructions."""
import json
import os
from pathlib import Path

import pytest

import agent_cli_sessions as C

GUIDE = '# Honua install: rules for AI coding agents\n\nÜnïcode stays byte-identical.\n'.encode()
TOOLS = {'tools': [{'name': 'honua_noop', 'description': 'No-op.', 'inputSchema': {'type': 'object', 'properties': {}}}]}


@pytest.fixture(autouse=True)
def _reset():
    C.set_agent_guide(None)
    yield
    C.set_agent_guide(None)


def recorder(tmp_path):
    """A fake CLI that records its argv, cwd, HOME and the working directory's files, then fails."""
    record = tmp_path / 'record.json'
    binary = tmp_path / 'fakecli'
    binary.write_text('#!/usr/bin/python3\nimport json, os, sys, pathlib\n'
                      'if sys.argv[1:] == ["--version"]:\n    print(os.environ.get("FAKE_VERSION", "x")); sys.exit(0)\n'
                      'if sys.argv[1:4] == ["debug", "models", "--bundled"]:\n'
                      '    print(json.dumps({"models": [{"slug": "gpt-6.1-sol"}]})); sys.exit(0)\n'
                      'cwd = pathlib.Path.cwd()\n'
                      f'json.dump({{"argv": sys.argv[1:], "cwd": str(cwd), "home": os.environ.get("HOME"), '
                      '"files": sorted(p.name for p in cwd.iterdir()), '
                      '"guide": (cwd / "AGENTS.md").read_bytes().decode() if (cwd / "AGENTS.md").exists() else None}, '
                      f'open({str(record)!r}, "w"))\nsys.exit(3)\n')
    binary.chmod(0o755)
    return binary, record


def run(tmp_path, monkeypatch, agent, model, guide):
    binary, record = recorder(tmp_path)
    root = tmp_path / 'guide-root'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(C, 'guide_scratch_root', lambda: root)
    monkeypatch.setattr(C, 'arc_scratch_root', lambda: root)
    C.set_agent_guide(guide)
    cli = C.CliModelSession(agent, model, binary=str(binary), auth_root=tmp_path / 'noauth')
    with pytest.raises(C.CliFailure):
        cli.run(prompt='Customer goal only', tool_view=TOOLS, execute=lambda *_: None, log_dir=tmp_path / 'log',
                timeout=600)
    return json.loads(record.read_text())


def test_claude_loads_exactly_the_staged_guide_from_its_own_directory(tmp_path, monkeypatch):
    seen = run(tmp_path, monkeypatch, 'claude-cli', 'claude-opus-5-5', GUIDE)
    argv = seen['argv']
    assert argv[argv.index('--setting-sources') + 1] == 'project'
    assert '--restricted' not in argv and argv[argv.index('--tools') + 1] == '' and '--strict-mcp-config' in argv
    assert seen['files'] == ['AGENTS.md'] and seen['guide'].encode() == GUIDE
    cwd, home = Path(seen['cwd']), Path(seen['home'])
    assert cwd.name == 'install' and home.name == 'home' and cwd.parent == home.parent   # siblings: no config above cwd
    assert home not in cwd.parents


def test_codex_loads_exactly_the_staged_guide_as_its_project_doc(tmp_path, monkeypatch):
    seen = run(tmp_path, monkeypatch, 'codex-cli', 'gpt-6.1-sol', GUIDE)
    overrides = dict(a.split('=', 1) for a in seen['argv'] if '=' in a and a.split('=', 1)[0] in (
        'project_doc_max_bytes', 'project_doc_fallback_filenames'))
    assert json.loads(overrides['project_doc_max_bytes']) == len(GUIDE)
    assert json.loads(overrides['project_doc_fallback_filenames']) == []
    assert '--ignore-user-config' in seen['argv'] and '--ignore-rules' in seen['argv']
    assert seen['files'] == ['AGENTS.md'] and seen['guide'].encode() == GUIDE


@pytest.mark.parametrize('agent,model', [('claude-cli', 'claude-opus-5-5'), ('codex-cli', 'gpt-6.1-sol')])
def test_without_a_guide_nothing_is_staged_and_loading_stays_disabled(tmp_path, monkeypatch, agent, model):
    seen = run(tmp_path, monkeypatch, agent, model, None)
    assert 'AGENTS.md' not in seen['files'] and seen['cwd'] == seen['home']
    if agent == 'claude-cli':
        assert seen['argv'][seen['argv'].index('--setting-sources') + 1] == '' and '--restricted' in seen['argv']
    else:
        assert '-c' in seen['argv'] and 'project_doc_max_bytes=0' in seen['argv']


@pytest.mark.parametrize('agent,planted', [('claude-cli', 'CLAUDE.md'), ('claude-cli', 'CLAUDE.local.md'),
                                           ('codex-cli', 'AGENTS.md'), ('codex-cli', 'AGENTS.override.md')])
def test_a_planted_user_or_global_instruction_file_refuses_the_session(tmp_path, agent, planted):
    scratch = tmp_path / 's'
    (scratch / 'home').mkdir(parents=True)
    config = scratch / 'home' / ('.claude' if agent == 'claude-cli' else '.codex')
    config.mkdir()
    (config / planted).write_text('Planted codeword: OSPREY-4410.\n')
    env = {'CLAUDE_CONFIG_DIR': str(config)} if agent == 'claude-cli' else {'CODEX_HOME': str(config)}
    with pytest.raises(ValueError, match='user or global instruction'):
        C.stage_agent_guide(scratch, GUIDE, agent, env)


@pytest.mark.parametrize('name', ['AGENTS.md', 'CLAUDE.md', '.git', '.claude/CLAUDE.md'])
def test_another_reachable_instruction_source_refuses_the_session(tmp_path, name):
    scratch = tmp_path / 'outer' / 's'
    (scratch / 'home' / '.claude').mkdir(parents=True)
    target = tmp_path / 'outer' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir() if name == '.git' else target.write_text('x')
    with pytest.raises(ValueError, match='other than the shipped agent guide'):
        C.stage_agent_guide(scratch, GUIDE, 'claude-cli', {'CLAUDE_CONFIG_DIR': str(scratch / 'home' / '.claude')})


def test_the_guide_scratch_root_must_be_private_real_disk_outside_home(tmp_path, monkeypatch):
    for bad in ('/tmp/x', '/var/tmp/x', '/home/ubuntu/x'):
        monkeypatch.setenv('HONUA_ARC_GUIDE_SCRATCH', bad)
        with pytest.raises(ValueError):
            C.guide_scratch_root()


def test_the_receipt_keeps_the_agent_guide_record():
    import inspect
    import ai_arc_runner as R
    assert "'authPreflight', 'agentGuide', 'cliFlags')}" in inspect.getsource(R)


@pytest.mark.parametrize('planted', ['.claude/settings.json', '.claude/settings.local.json', '.claude/commands/x.md',
                                     '.claude/agents/x.md', '.claude/skills/x/SKILL.md', '.mcp.json', 'CLAUDE.md'])
def test_a_planted_install_dir_entry_refuses_the_session(tmp_path, monkeypatch, planted):
    """Condition (a)/(b): anything besides AGENTS.md in the working directory (e.g. a .claude/settings.json carrying a
    hook) refuses the session before the CLI starts."""
    binary, record = recorder(tmp_path)
    root = tmp_path / 'guide-root'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(C, 'guide_scratch_root', lambda: root)
    real = C.write_private

    def write_and_plant(path, raw):
        real(path, raw)
        if Path(path).name == 'AGENTS.md':
            target = Path(path).parent / planted
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [{'type': 'command',
                                                                                 'command': 'touch /tmp/pwned'}]}]}}))
    monkeypatch.setattr(C, 'write_private', write_and_plant)
    C.set_agent_guide(GUIDE)
    cli = C.CliModelSession('claude-cli', 'claude-opus-5-5', binary=str(binary), auth_root=tmp_path / 'noauth')
    with pytest.raises(C.CliFailure):
        cli.run(prompt='Customer goal only', tool_view=TOOLS, execute=lambda *_: None, log_dir=tmp_path / 'log',
                timeout=600)
    assert not record.exists()                                   # the CLI never started


@pytest.mark.parametrize('name', ['.claude', '.mcp.json', 'CLAUDE.local.md', '.codex'])
def test_any_claude_dir_or_mcp_file_in_an_ancestor_refuses(tmp_path, name):
    scratch = tmp_path / 'outer' / 's'
    (scratch / 'home' / '.claude').mkdir(parents=True)
    target = tmp_path / 'outer' / name
    target.mkdir() if name in ('.claude', '.codex') else target.write_text('{}')
    with pytest.raises(ValueError, match='other than the shipped agent guide'):
        C.stage_agent_guide(scratch, GUIDE, 'claude-cli', {'CLAUDE_CONFIG_DIR': str(scratch / 'home' / '.claude')})


def test_the_receipt_provenance_records_the_effective_flags(tmp_path, monkeypatch):
    seen = run(tmp_path, monkeypatch, 'claude-cli', 'claude-opus-5-5', GUIDE)
    flags = C.effective_flags(['claude', *seen['argv']], 'never-used-contract')
    assert '--setting-sources' in flags and 'project' in flags and '--restricted' not in flags
    assert not any(f.startswith('/') for f in flags)


def test_the_workflow_prepares_the_guide_root_on_the_controller_runner():
    text = (Path(__file__).resolve().parents[1] / '.github/workflows/ai-arc.yml').read_text()
    assert 'install -d -m 700' in text and '/var/lib/honua-arc-cli' in text
