"""P15 can consume only the previous agent-published service."""
from types import SimpleNamespace

import pytest
from agent_e2e_local_scenarios.arc_p15 import ArcP15


class Recorder:
    principals = {'agent': ('unused', 'agent-id')}
    def __init__(self): self.calls = []
    def json(self, step, method, path, body=None):
        self.calls.append((step, method, path))
        if step == 'arc-p15-service-meta':
            value = {'serviceName': 'agent-service', 'layers': [{'id': 2}]}
        elif step == 'p15-map-draft':
            value = {'data': {'draftId': '11111111-1111-4111-8111-111111111111'}}
        elif step == 'p15-map-saved':
            value = {'data': {'itemId': '22222222-2222-4222-8222-222222222222',
                              'versionId': '33333333-3333-4333-8333-333333333333', 'contentHash': 'a' * 64}}
        else:
            value = {'features': []}
        return {'observation': {'httpStatus': 201 if step in ('p15-map-draft', 'p15-map-saved') else 200,
                                'body': value}}


def test_raw_p15_seed_uses_only_prior_agent_service():
    rec = Recorder()
    shared = {'variant': 'A', 'service': 'agent-service', 'layerId': 2, 'caseReceiptSha256': 'sha256:' + 'a' * 64}
    stack = SimpleNamespace(compose_project='arc-p15', extra={'recorder': rec, 'arcSharedService': shared,
                                                              'addPrincipal': lambda *_: None})
    seeded = ArcP15().seed(stack, 'A')
    assert seeded.state['shelters'] == {'service': 'agent-service', 'layerId': 2}
    assert seeded.state['priorCaseReceiptSha256'] == shared['caseReceiptSha256']
    assert all('service.publish' not in call[2] for call in rec.calls)


def test_raw_p15_rejects_missing_or_foreign_prior_service():
    rec = Recorder()
    stack = SimpleNamespace(compose_project='arc-p15', extra={'recorder': rec, 'addPrincipal': lambda *_: None})
    with pytest.raises(ValueError, match='prior agent publication'):
        ArcP15().seed(stack, 'A')
    stack.extra['arcSharedService'] = {'variant': 'A', 'service': 'foreign', 'layerId': 2,
                                      'caseReceiptSha256': 'sha256:' + 'a' * 64}
    with pytest.raises(ValueError, match='not live'):
        ArcP15().seed(stack, 'A')
