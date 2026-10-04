"""Every job of nightly-certification.yml that runs a tool installs tools/requirements-nightly.txt,
and that file alone satisfies every import those tools reach (honua-release#376, R18).

Run 37132168180 died in `resolve` on `ModuleNotFoundError: cryptography`: verify_client_artifacts
imports it inside a function, three modules below the resolver, so importing the entry tool never
saw it. This walks the local import graph of each tool the workflow runs (function-level imports
and the certification/ modules the resolver puts on sys.path included), installs the requirements
file alone into an isolated target, and imports every module of that graph plus every third-party
module it names in an interpreter that sees no site-packages.
"""
import ast
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/nightly-certification.yml'
REQUIREMENTS = ROOT / 'tools/requirements-nightly.txt'
INSTALL = 'python -m pip install -r tools/requirements-nightly.txt'
LOCAL_DIRS = (ROOT / 'tools', ROOT / 'certification')
TOOL_CALL = re.compile(r'\bpython3? (tools/[A-Za-z0-9_]+)\.py\b')
HEREDOC = re.compile(r"python3? - <<'(\w+)'[^\n]*\n(.*?)\n\s*\1\s*$", re.S | re.M)


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())['jobs']


def _runs(job):
    return [step['run'] for step in job.get('steps', []) if 'run' in step]


def _entry_tools():
    return sorted({Path(m).name for job in _jobs().values() for run in _runs(job) for m in TOOL_CALL.findall(run)})


def _inline_scripts():
    return [textwrap.dedent(body) for job in _jobs().values() for run in _runs(job)
            for _, body in HEREDOC.findall(run)]


def _local(name):
    return next((d / f'{name}.py' for d in LOCAL_DIRS if (d / f'{name}.py').is_file()), None)


def _imports(source):
    """Top-level names of every absolute import anywhere in the module, function bodies included."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split('.')[0])
    return names


def _graph():
    """(local modules reached, third-party top-level names) from the workflow's tools and inline scripts."""
    local, third_party = set(), set()
    pending = list(_entry_tools())
    for script in _inline_scripts():
        pending.extend(_imports(script))
    while pending:
        name = pending.pop()
        if name in local or name in third_party or name in sys.stdlib_module_names:
            continue
        path = _local(name)
        if path is None:
            third_party.add(name)
            continue
        local.add(name)
        pending.extend(_imports(path.read_text()))
    return local, third_party


def test_workflow_runs_tools_and_the_graph_reaches_the_lazy_import_that_broke_the_night():
    tools = _entry_tools()
    assert {'resolve_trunk_candidate', 'mint_nightly_lock', 'validate_platform', 'nightly_receipts',
            'convergence_rebind'} <= set(tools)
    local, third_party = _graph()
    assert 'verify_client_artifacts' in local and 'check_build_test' in local
    assert {'yaml', 'jsonschema', 'cryptography'} <= third_party


@pytest.mark.parametrize('job_name', sorted(_jobs()))
def test_every_job_that_runs_python_installs_only_the_pinned_requirements(job_name):
    job = _jobs()[job_name]
    runs = _runs(job)
    if not any(TOOL_CALL.search(run) or HEREDOC.search(run) for run in runs):
        return
    installs = [run for run in runs if 'pip install' in run]
    assert installs == [INSTALL], f'{job_name} must install exactly {INSTALL!r}, found {installs!r}'
    steps = job['steps']
    first_python = next(i for i, step in enumerate(steps)
                        if TOOL_CALL.search(step.get('run', '')) or HEREDOC.search(step.get('run', '')))
    assert next(i for i, step in enumerate(steps) if step.get('run') == INSTALL) < first_python


def test_requirements_are_exact_pins():
    lines = [line.split('#')[0].strip() for line in REQUIREMENTS.read_text().splitlines()]
    pins = [line for line in lines if line]
    assert pins and all(re.fullmatch(r'[A-Za-z0-9._-]+==[A-Za-z0-9._+-]+', pin) for pin in pins), pins


def test_requirements_alone_import_everything_the_nightly_tools_reach(tmp_path):
    target = tmp_path / 'site'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check',
                    '--no-input', '--no-deps', '--target', str(target), '-r', str(REQUIREMENTS)],
                   check=True)
    local, third_party = _graph()
    probe = textwrap.dedent(f'''
        import importlib, sys
        assert not any('-packages' in p for p in sys.path), sys.path
        sys.path[:0] = {[str(target)] + [str(d) for d in LOCAL_DIRS]!r}
        failures = []
        for name in {sorted(third_party) + sorted(local)!r}:
            try:
                importlib.import_module(name)
            except Exception as error:
                failures.append(f'{{name}}: {{type(error).__name__}}: {{error}}')
        if failures:
            raise SystemExit('\\n'.join(failures))
    ''')
    # -I ignores PYTHONPATH and the user site; -S skips site-packages entirely: only stdlib + target.
    result = subprocess.run([sys.executable, '-I', '-S', '-c', probe], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
