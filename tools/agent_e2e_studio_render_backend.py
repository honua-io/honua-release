"""Bind a frozen render declaration to one original model save, then observe it.

Only native draft/version identities join from the authenticated save lineage.
Expected content, source features, styles and pixels remain source-frozen.
"""
from __future__ import annotations

import copy
import re

from agent_e2e_native_authoring_bindings import contract as binding_contract, replay
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_observer import guid
from agent_e2e_studio_render import project


def require(value, reason):
    if not value:
        raise PackError(reason)


def validate(value, *, studio, declarations, candidate):
    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'savedVersionBinding', 'contract', 'observer'}
            and value['schemaVersion'] == 1 and isinstance(value['savedVersionBinding'], str)
            and value['savedVersionBinding'] in declarations and isinstance(value['observer'], dict) and value['observer'],
            'rendering requires an explicit frozen saved-version binding and private browser observer')
    declaration = declarations[value['savedVersionBinding']]
    binding_contract(declaration)
    relations = declaration['relationships']
    require(all(relations[key] == studio[key] for key in ('itemId', 'namespace', 'ownerPrincipalId', 'family'))
            and studio['family'] == 'map', 'rendering binding differs from the exact authored item/owner/workspace/family')
    template = value['contract']
    require(isinstance(template, dict) and set(template) == {'schemaVersion', 'candidateKey', 'consoleCommit', 'endpoint',
            'session', 'scope', 'runtimeAssets', 'mapLibreModule', 'width', 'height', 'background', 'backgroundTolerance',
            'layers', 'regions'} and template['schemaVersion'] == 1 and template['candidateKey'] == candidate,
            'rendering declaration differs from the frozen candidate contract')
    scope = template['scope']
    require(isinstance(scope, dict) and set(scope) == {'itemId', 'namespace', 'ownerPrincipalId', 'documentSha256'}
            and all(scope[key] == studio[key] for key in ('itemId', 'namespace', 'ownerPrincipalId'))
            and isinstance(scope['documentSha256'], str) and re.fullmatch(r'[0-9a-f]{64}', scope['documentSha256']),
            'rendering must freeze expected content; only original draft/version IDs may join at runtime')
    require(isinstance(template['layers'], list) and template['layers'] and isinstance(template['regions'], list)
            and template['regions'], 'rendering lacks independently frozen feature/style/pixel predicates')


def bind(value, *, studio, declarations, reports, context, selection):
    validate(value, studio=studio, declarations=declarations, candidate=context['componentBindingDigest'])
    name = value['savedVersionBinding']
    require(name in reports, 'rendering lacks the original saved-version evidence')
    report = reports[name]
    identities = replay(report, declarations[name], context)
    require(report['selection'] == {key: selection[key] for key in ('ordinal', 'actionId', 'request', 'publicResult')},
            'rendering selected a different model save than the original native evidence')
    saved = selection['resource']
    require(saved['versionId'] == identities['savedVersionId'] and
            canonical_digest(saved['envelope']['body']) == 'sha256:'+value['contract']['scope']['documentSha256'],
            'model content differs from the independently frozen render document')
    contract = copy.deepcopy(value['contract'])
    contract['scope'].update(draftId=guid(saved['sourceDraftId']), versionId=guid(identities['savedVersionId']))
    return contract


def observe(value, *, studio, declarations, reports, context, selection, durable):
    contract = bind(value, studio=studio, declarations=declarations, reports=reports, context=context, selection=selection)
    try:
        from hosted_pilot.browser_render import observe_original_render
    except ImportError:
        raise PackError('private signed-in browser observer runtime is unavailable') from None
    receipt = observe_original_render(contract=contract, selection=selection, durable=durable,
        native_binding={'name': value['savedVersionBinding'], 'declaration': declarations[value['savedVersionBinding']],
                        'report': reports[value['savedVersionBinding']]}, context=context, configuration=value['observer'])
    require(isinstance(receipt, dict) and set(receipt) == {'report', 'reference'},
            'private browser observer did not retain its original report')
    reference = receipt['reference']
    require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'} and
            canonical_digest(durable.state.get('scenarioOperations', {}).get(reference['key'])) == reference['sha256'],
            'private browser report custody is absent or changed')
    retained = durable.state['scenarioOperations'][reference['key']]
    require(retained.get('producer') == 'private-browser-render' and
            retained.get('context') == {key: context[key] for key in
                ('campaignId', 'caseId', 'attempt', 'campaignMode', 'componentBindingDigest')} and
            retained.get('candidateKey') == context['componentBindingDigest'] and
            retained.get('report') == receipt['report'] and retained.get('contractDigest') == canonical_digest(contract),
            'private browser original differs from the exact candidate render contract')
    return {'actual': project(receipt['report'], contract), 'contract': contract,
            'reportReference': reference, 'savedBindingDigest': canonical_digest(reports[value['savedVersionBinding']])}
