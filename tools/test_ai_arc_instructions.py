import hashlib
import json
from pathlib import Path

import pytest

import ai_arc_instructions as I


def package(tmp_path):
    files = {'AGENTS.md': b'Canonical guide\n'}
    for name in I.SKILLS:
        files[f'skills/{name}/SKILL.md'] = f'---\nname: {name}\n---\nTask\n'.encode()
        files[f'skills/{name}/references/task.md'] = b'Procedure\n'
    pins = {}
    for name, raw in files.items():
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        if name.startswith('skills/'):
            pins[name] = I.digest(raw)
    manifest = ''.join(f'{hashlib.sha256(raw).hexdigest()}  {name}\n' for name, raw in sorted(files.items()))
    (tmp_path / 'agent-files.sha256').write_text(manifest)
    return files, {'files': pins}


def test_bundle_preserves_guide_and_includes_all_pinned_files(tmp_path):
    files, pin = package(tmp_path)
    raw, facts = I.load_bundle(tmp_path, pin)
    assert raw.startswith(files['AGENTS.md'])
    assert facts['renderedSha256'] == I.digest(raw)
    assert facts['files'] == {name: I.digest(value) for name, value in files.items()}
    assert set(facts['skills']) == set(I.SKILLS)
    assert all(value in raw for value in files.values())


@pytest.mark.parametrize('damage', ['tampered', 'unpinned', 'missing', 'symlink', 'traversal', 'duplicate'])
def test_bundle_rejects_invalid_custody(tmp_path, damage):
    files, pin = package(tmp_path)
    name = next(name for name in files if name.startswith('skills/'))
    path = tmp_path / name
    if damage == 'tampered':
        path.write_bytes(b'Changed')
    elif damage == 'unpinned':
        del pin['files'][name]
    elif damage == 'missing':
        path.unlink()
    elif damage == 'symlink':
        path.unlink()
        path.symlink_to(tmp_path / 'AGENTS.md')
    else:
        with (tmp_path / 'agent-files.sha256').open('a') as manifest:
            manifest.write(f'{"a" * 64}  {"../outside" if damage == "traversal" else name}\n')
    with pytest.raises(ValueError):
        I.load_bundle(tmp_path, pin)


def test_held_out_and_nonlocal_variants_fail_closed():
    I.validate_variant('guide-only', 'H', 'aws-ecs')
    I.validate_variant('guide-plus-skills', 'A', 'local-docker')
    for variant, goal, target in [('unknown', 'A', 'local-docker'),
                                  ('guide-plus-skills', 'H', 'local-docker'),
                                  ('guide-plus-skills', 'B', 'aws-ecs')]:
        with pytest.raises(ValueError):
            I.validate_variant(variant, goal, target)


@pytest.mark.parametrize('damage', ['missing', 'unexpected', 'held-out', 'tampered'])
def test_receipt_requires_independently_rebuilt_skill_provenance(tmp_path, damage):
    files, pin = package(tmp_path)
    _, facts = I.load_bundle(tmp_path, pin)
    receipt = {'instructionVariant': 'guide-plus-skills', 'variant': 'A', 'target': 'local-docker',
               'slotFacts': {'quickstart': {'packageSha256': {'AGENTS.md': I.digest(files['AGENTS.md'])},
                                            'instructionVariant': facts}}}
    rebuilt = {'instructionVariant': json.loads(json.dumps(facts))}
    if damage == 'missing':
        del receipt['slotFacts']['quickstart']['instructionVariant']
    elif damage == 'unexpected':
        receipt['instructionVariant'] = 'guide-only'
    elif damage == 'held-out':
        receipt['variant'] = 'H'
    else:
        receipt['slotFacts']['quickstart']['instructionVariant']['renderedSha256'] = 'sha256:' + '0' * 64
    with pytest.raises(ValueError):
        I.validate_receipt(receipt, rebuilt)


def test_receipt_accepts_rebuilt_bundle_and_historical_guide_only(tmp_path):
    files, pin = package(tmp_path)
    _, facts = I.load_bundle(tmp_path, pin)
    receipt = {'instructionVariant': 'guide-plus-skills', 'variant': 'B', 'target': 'local-docker',
               'slotFacts': {'quickstart': {'packageSha256': {'AGENTS.md': I.digest(files['AGENTS.md'])},
                                            'instructionVariant': facts}}}
    I.validate_receipt(receipt, {'instructionVariant': facts})
    I.validate_receipt({'variant': 'H', 'target': 'aws-ecs'}, None)
    with pytest.raises(ValueError):
        I.validate_receipt(receipt, None)


@pytest.mark.parametrize('damage', ['receipt-only-relabel', 'missing', 'restricted', 'wrong-summary'])
def test_skill_sessions_require_retained_instruction_evidence(tmp_path, damage):
    row = {'stage': 'setup', 'modelVisible': {'provenance': [
        {'agentGuide': {'sha256': 'skills', 'loaded': True}, 'cliFlags': []}]}}
    launched = {'agentGuide': {'sha256': 'skills', 'loaded': True}, 'cliFlags': []}
    if damage == 'receipt-only-relabel':
        launched['agentGuide']['sha256'] = 'baseline'
    elif damage == 'restricted':
        launched['cliFlags'] = ['--restricted']
    elif damage == 'wrong-summary':
        row['modelVisible']['provenance'][0]['cliFlags'] = ['other']
    sessions = [{'instructionProvenance': [launched]}]
    if damage == 'missing':
        sessions = [{'cliVersion': 'pinned'}]
    (tmp_path / 'setup').mkdir()
    (tmp_path / 'setup' / 'context.json').write_text(json.dumps({'sessions': sessions}))
    with pytest.raises(ValueError, match='instruction'):
        I.verify_sessions(row, tmp_path, 'skills')


def test_skill_sessions_bind_loaded_digest_and_flags_to_retained_evidence(tmp_path):
    import ai_arc_runner as R
    provenance = [{'cliVersion': 'pinned', 'agentGuide': {'sha256': 'skills', 'loaded': True}, 'cliFlags': []}]
    row = {'stage': 'setup', 'modelVisible': {'provenance': provenance}}
    (tmp_path / 'setup').mkdir()
    session = R.session_record({'provenance': provenance, 'actions': [{}]})
    (tmp_path / 'setup' / 'context.json').write_text(json.dumps({'sessions': [session]}))
    I.verify_sessions(row, tmp_path, 'skills')
