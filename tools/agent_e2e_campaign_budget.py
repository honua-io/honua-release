"""Bind a frozen campaign to original accounting; never create an allowance."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

from agent_e2e_bootstrap import validate_import
from agent_e2e_budget import FOSS4G_POLICY, money, validate as validate_budget
from agent_e2e_pack import PackError, digest_bytes

ORIGINAL = 'budget-bootstrap-original.json'


def require(value, message):
    if not value:
        raise PackError(message)


def declaration(value):
    require(isinstance(value, dict) and set(value) == {'policyId', 'priorLedgerDigest'} and
            value['policyId'] == FOSS4G_POLICY and isinstance(value['priorLedgerDigest'], str) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', value['priorLedgerDigest']),
            'campaign budget requires the exact named owner amendment and original history digest')
    return copy.deepcopy(value)


def original_file(path, expected, candidate):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 8*1024*1024,
            'original bootstrap funding artifact missing or oversized')
    raw = path.read_bytes()
    require(digest_bytes(raw) == expected, 'original bootstrap funding bytes changed')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'duplicate original bootstrap field')
            value[key] = item
        return value
    imported = {'artifactDigest': expected, 'artifact': json.loads(raw, object_pairs_hook=unique)}
    validate_import(imported, candidate)
    return imported, raw


def freeze_policy(config, candidate, artifact, authenticated):
    """Authentication occurs through verify_import before this pure projection."""
    policy = declaration(config['budgetPolicy'])
    reference = config['bootstrap']
    require(set(reference) == {'artifactDigest', 'sourceSha', 'runId', 'runAttempt'},
            'amended freeze requires the original authenticated bootstrap producer reference')
    require(isinstance(reference['sourceSha'], str) and re.fullmatch(r'[a-f0-9]{40}', reference['sourceSha']) and
            all(type(reference[name]) is int and reference[name] > 0 for name in ('runId', 'runAttempt')),
            'amended freeze original producer identity is malformed')
    imported, raw = original_file(artifact, reference['artifactDigest'], candidate)
    require(authenticated == imported and all(imported['artifact']['source'][name] == reference[name]
            for name in ('sourceSha', 'runId', 'runAttempt')),
            'amended freeze is not the authenticated original bootstrap producer')
    ledger = imported['artifact']['ledger']
    observed = ledger['schemaVersion'] == 3
    if observed:
        require(policy == {name: ledger['policyAmendment'][name] for name in policy},
                'campaign policy differs from original bootstrap amendment')
    # A v2 original is an expected amendment intent. Only actual runtime/gate
    # observation of the named v3 amendment can admit execution/qualification.
    return {**policy, 'original': {'path': ORIGINAL, 'sha256': reference['artifactDigest']},
            'amendmentObservedAtFreeze': observed}, raw


def manifest_policy(campaign):
    value = campaign.get('budgetPolicy')
    if value is None:
        require('budgetPolicy' not in campaign and money(campaign['totalBudgetUsd']) == 500,
                'legacy campaign budget must remain $500 without an amendment binding')
        return None
    require(isinstance(value, dict) and set(value) == {'policyId', 'priorLedgerDigest', 'original', 'amendmentObservedAtFreeze'} and
            type(value['amendmentObservedAtFreeze']) is bool and
            isinstance(value['original'], dict) and set(value['original']) == {'path', 'sha256'} and
            value['original']['path'] == ORIGINAL and isinstance(value['original']['sha256'], str) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', value['original']['sha256']) and money(campaign['totalBudgetUsd']) == 2000,
            'frozen campaign amendment or original funding reference changed')
    declaration({name: value[name] for name in ('policyId', 'priorLedgerDigest')})
    return value


def bind_ledger(campaign, ledger, root, *, bootstrap=None):
    """Replay the original history and actual amendment before paid admission."""
    summary = validate_budget(ledger)
    require(ledger['candidateKey'] == campaign['componentBindingDigest'], 'budget belongs to another candidate')
    policy = manifest_policy(campaign)
    if policy is None:
        require(ledger['schemaVersion'] == 2, 'legacy campaign cannot adopt an unbound amended allowance')
        return summary
    require(ledger['schemaVersion'] == 3 and
            all(ledger['policyAmendment'][name] == policy[name] for name in ('policyId', 'priorLedgerDigest')),
            'actual durable ledger lacks the frozen named owner amendment')
    imported, _ = original_file(Path(root)/ORIGINAL, policy['original']['sha256'], ledger['candidateKey'])
    if 'interactiveAccounting' in ledger:
        marker = ledger['interactiveAccounting']
        require(marker['bootstrapArtifactDigest'] == imported['artifactDigest'] and
                marker['source'] == imported['artifact']['source'],
                'interactive history belongs to different original funding')
    if bootstrap is not None:
        require(bootstrap == imported, 'actual durable import differs from original frozen funding artifact')
    original = imported['artifact']['ledger']
    require(policy['amendmentObservedAtFreeze'] == (original['schemaVersion'] == 3),
            'frozen amendment observation does not match original bootstrap')
    if original['schemaVersion'] == 3:
        require(all(original['policyAmendment'][name] == policy[name] for name in ('policyId', 'priorLedgerDigest')),
                'original funding amendment changed')
    from agent_e2e_state import preserve_history
    if original['schemaVersion'] == 2:
        # Charges may have accumulated after the original import and before
        # the actual amendment. Replay both append-only transitions, rather
        # than pretending the earlier bootstrap snapshot was that later basis.
        prior = ledger['policyAmendment']['priorLedger']
        preserve_history({'ledger': original}, {'ledger': prior})
        preserve_history({'ledger': prior}, {'ledger': ledger})
    else:
        preserve_history({'ledger': original}, {'ledger': ledger})
    return summary
