"""Pinned instruction-content variants for development AI-arc runs."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

SKILLS = ('honua-install', 'honua-services', 'honua-studio-maps', 'honua-ops', 'honua-tuning')
VARIANTS = ('guide-only', 'guide-plus-skills')


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def validate_variant(name, goal_variant, target):
    if name not in VARIANTS:
        raise ValueError('unknown instruction variant')
    if name == 'guide-plus-skills' and (goal_variant == 'H' or target != 'local-docker'):
        raise ValueError('skill instructions require a local-docker development run')


def manifest_entries(root):
    manifest = root / 'agent-files.sha256'
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('missing regular agent file manifest')
    entries = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})  (.+)', line)
        if not match:
            raise ValueError('invalid agent file manifest')
        checksum, name = match.groups()
        parts = PurePosixPath(name).parts
        if (name in entries or not parts or name != '/'.join(parts) or
                any(part in ('.', '..') for part in parts) or name.startswith('/') or '\\' in name):
            raise ValueError('unsafe or duplicate agent file path')
        if name != 'AGENTS.md' and (len(parts) < 3 or parts[0] != 'skills' or parts[1] not in SKILLS):
            raise ValueError('unexpected agent file')
        entries[name] = 'sha256:' + checksum
    required = {'AGENTS.md', *(f'skills/{skill}/SKILL.md' for skill in SKILLS)}
    if not required.issubset(entries):
        raise ValueError('all five operating skills and AGENTS.md are required')
    return entries


def read_pinned(root, name, checksum):
    path = root
    for part in PurePosixPath(name).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('symlink in instruction package')
    if not path.is_file():
        raise ValueError('missing instruction file')
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or digest(raw) != checksum:
        raise ValueError('instruction file differs from its digest')
    raw.decode('utf-8')
    return raw


def load_bundle(root, pin):
    root = Path(root)
    entries = manifest_entries(root)
    pinned = {name: checksum for name, checksum in pin['files'].items() if name.startswith('skills/')}
    actual = {path.relative_to(root).as_posix() for path in (root / 'skills').rglob('*') if path.is_file()}
    expected = {name for name in entries if name.startswith('skills/')}
    if actual != expected or pinned != {name: entries[name] for name in expected}:
        raise ValueError('skill package must exactly match digest-pinned source files')
    files = {name: read_pinned(root, name, checksum) for name, checksum in sorted(entries.items())}
    raw = files['AGENTS.md']
    for name, contents in files.items():
        if name != 'AGENTS.md':
            raw += f'\n\n---\nOperating skill file: {name}\n\n'.encode() + contents
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('instruction bundle exceeds limit')
    return raw, {'name': 'guide-plus-skills', 'skills': list(SKILLS), 'files': entries,
                 'renderedSha256': digest(raw), 'loading': 'project-instructions'}


def apply_bundle(ctx, package):
    from agent_cli_sessions import require_agent_guide, set_agent_guide
    root = ctx.private_dir / 'setup' / 'package-build' / 'package'
    raw, facts = load_bundle(root, ctx.arc['quickstartPackage'])
    if facts['files']['AGENTS.md'] != digest(package['AGENTS.md']):
        raise ValueError('skill bundle guide differs from staged package')
    set_agent_guide(raw)
    require_agent_guide(facts['renderedSha256'])
    ctx.slot.facts['quickstart']['instructionVariant'] = facts


def expected_guide(quickstart):
    shipped = (quickstart.get('packageSha256') or {}).get('AGENTS.md')
    facts = quickstart.get('instructionVariant')
    if facts is None:
        return shipped
    files = facts.get('files') or {}
    if (facts.get('name') != 'guide-plus-skills' or facts.get('loading') != 'project-instructions' or
            facts.get('skills') != list(SKILLS) or files.get('AGENTS.md') != shipped or
            any(f'skills/{skill}/SKILL.md' not in files for skill in SKILLS) or
            not re.fullmatch(r'sha256:[a-f0-9]{64}', facts.get('renderedSha256', ''))):
        raise ValueError('invalid skill instruction provenance')
    return facts['renderedSha256']


def validate_receipt(receipt, package):
    name = receipt.get('instructionVariant', 'guide-only')
    validate_variant(name, receipt.get('variant'), receipt.get('target'))
    quickstart = (receipt.get('slotFacts') or {}).get('quickstart') or {}
    facts = quickstart.get('instructionVariant')
    if name == 'guide-only':
        if facts is not None:
            raise ValueError('guide-only receipt carries unexpected skill provenance')
        return
    if facts is None or not package or facts != package.get('instructionVariant'):
        raise ValueError('skill provenance differs from independently rebuilt package')
    expected_guide(quickstart)


def instruction_provenance(provenance):
    """Keep the launched instruction digest and flags in retained stage evidence."""
    return [{key: item.get(key) for key in ('agentGuide', 'cliFlags')}
            for item in provenance if isinstance(item, dict)]


def verify_sessions(row, folder, quickstart):
    """A receipt cannot relabel sessions that launched with another instruction bundle."""
    context = json.loads((folder / row['stage'] / 'context.json').read_bytes())
    sessions = [session for session in context.get('sessions') or []
                if not (session.get('actions') == 0 and session.get('provenanceEntries') == 0)]
    if (quickstart.get('instructionVariant') is None and sessions
            and all('instructionProvenance' not in session for session in sessions)):
        return  # Historical baseline contexts predate retained instruction provenance.
    shipped = expected_guide(quickstart)
    retained = []
    for session in sessions:
        claims = session.get('instructionProvenance')
        if not isinstance(claims, list) or not claims:
            raise ValueError('retained session lacks instruction provenance')
        retained.extend(claims)
    summary = instruction_provenance((row.get('modelVisible') or {}).get('provenance') or [])
    if retained != summary:
        raise ValueError('receipt instruction provenance contradicts retained sessions')
    expected = {'sha256': shipped, 'loaded': True} if shipped else None
    for claim in retained:
        if (claim.get('agentGuide') != expected
                or (shipped and '--restricted' in (claim.get('cliFlags') or []))):
            raise ValueError('retained session launched different instructions')
