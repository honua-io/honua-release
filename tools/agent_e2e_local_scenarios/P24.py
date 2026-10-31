"""P24 zero to a published map.

Style and rendering use controller reads. Published sources bind to the customer's
server audit events, captured server publication receipts, and the saved map. Missing
or ambiguous evidence fails closed. Other unsupported facts also fail closed on local
Docker. Pack prompts stay in the pack.
"""
from __future__ import annotations

from agent_browser_studio import (
    handoff_spec, observe_map, render_spec, seed_case, source_content_spec, style_spec,
)
from agent_e2e_local_api import Observation, RoleBinding, Scenario

# Steps the local runner does not record. Naming them fails the projection closed.
_CANDIDATE = 'p24-unobserved-candidate'
_DEPLOYMENT = 'p24-unobserved-deployment'
_FILTER = 'p24-unobserved-filter'
_BUFFER = 'p24-unobserved-buffer'
_SESSION = 'p24-unobserved-session'
_APPROVAL = 'p24-unobserved-approval'
_PUBLICATION = 'p24-unobserved-publication'


def _missing(step, pointer):
    return {'body': step, 'pointer': pointer}


def _object(step, fields):
    return {'object': {name: _missing(step, '/' + name) for name in fields}}


def _custody(fields):
    return {'object': {name: {'custody': name} for name in fields}}


def _features_exact():
    # Tolerance belongs to the frozen expectation; the oracle needs only live CRS and features.
    return {'featureServerFeatures': 'p24-src-shelters-after-raw', 'booleanFields': ['is_open'],
            'booleanGeo': {'p24-src-shelters-after-raw': 'p24-src-shelters-after-geo'}}


class P24(Scenario):
    id = 'P24'
    supported_variants = ('A', 'B')

    def needs_approver(self):
        return True

    def seed(self, stack, variant):
        return seed_case(self.id, stack, variant, create_map=False)

    def roles(self, stack, seeded, variant, run=None):
        return [
            RoleBinding('created:map', 'p24-maps', items='/data/items', id='/itemId',
                        owner='/ownerId', before='p24-maps-before',
                        owner_any_of=['{agentUser}', '{agentKey}', 'api-key:{agentKey}']),
            RoleBinding('created:version', 'p24-maps', items='/data/items', id='/currentVersionId',
                        owner='/ownerId', before='p24-maps-before',
                        owner_any_of=['{agentUser}', '{agentKey}', 'api-key:{agentKey}']),
        ] + [RoleBinding(f'audited:{kind}:{name}', 'p24-version', parent='created:map',
                         before='p24-maps-before')
             for name in ('parcels', 'flood_zones', 'shelters') for kind in ('service', 'layer')]

    def observe(self, stack, seeded, variant, run=None):
        observe_map(stack, seeded, 'p24', created=True)
        specs = {
            'candidate.identity': _object(_CANDIDATE, (
                'componentBindingDigest', 'serverSourceRevision', 'serverOciDigest', 'clientArtifactDigests')),
            'deployment.ready': _object(_DEPLOYMENT, (
                'deploymentId', 'healthStatus', 'authenticatedReadStatus')),
            'features.exact': _features_exact(),
            'filter.exact': _object(_FILTER, ('resourceIds',)),
            'buffer.metric': _object(_BUFFER, (
                'sourceId', 'center3857', 'radiusMetres', 'expectedArea',
                'radialToleranceMetres', 'areaRelativeTolerance')),
            'map.style': style_spec('p24', 'created:map'),
            'state.reopened': {'object': {
                'resourceId': {'target': 'p24-version', 'role': 'created:map', 'at': {'pathSegment': 4}},
                'versionId': {'target': 'p24-version', 'role': 'created:version', 'at': {'pathSegment': 6}},
                'documentSha256': {'body': 'p24-saved', 'pointer': '/documentSha256'},
                'freshSessionId': _missing(_SESSION, '/freshSessionId'),
            }},
            'approval.separate': _object(_APPROVAL, (
                'proposalId', 'requesterPrincipalId', 'approverPrincipalId', 'approvedVersionId', 'auditId')),
            'publication.public': _object(_PUBLICATION, (
                'publishedUrl', 'anonymousHttpStatus', 'resourceId', 'publishedVersionId')),
            'render.actual': render_spec('p24'),
            'custody.bootstrap-revoked': _custody((
                'bootstrapPrincipalId', 'revokedCredentialIds', 'postRevocationProbeStatus')),
            'custody.worker-replaced': _custody((
                'bootstrapWorkerId', 'taskWorkerId', 'bootstrapWorkerTerminalState',
                'taskPrincipalId', 'taskProvisioningCredentialIds')),
            'source-content.unchanged': source_content_spec('p24'),
        }
        return [Observation(name, None, {'projection': 'extract/v1', 'spec': specs[name]}) for name in specs]

    def handoff(self, stack, seeded, variant, run=None):
        return handoff_spec('p24', 'created:map')


scenario = P24()
