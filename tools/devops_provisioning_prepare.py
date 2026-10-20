"""Preparation of the original AWS provisioning proof, without an actuator.

Reuse the maintained complete local certification consumer. A final release report
(which itself requires cloud parity) is deliberately not a prerequisite. No rows,
licensed lanes or required facets are filtered from the original requirements.
The executable native driver separately authenticates published artifact originals;
this module cannot turn a caller-written native seal into authority.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import re
from pathlib import Path
from typing import Mapping
import check_protocol_certification as protocol
from devops_provisioning_contract import CELLS, Refusal, require


@dataclass(frozen=True)
class CandidateContext:
    source_sha: str
    image_digest: str
    cut_at: str
    component_source_shas: Mapping[str,str]
    client_versions: Mapping[str,str]


def local_client_prerequisite(ledger: dict, receipt_root: Path,
                              candidate: CandidateContext, *, now: datetime):
    """Rejudge local157 against the fixed maintained catalog and original receipts.

    The caller cannot replace/filter requirements. The authenticated producer
    context must separately bind this verifier/catalog source and ledger origin.
    """
    catalog,catalog_error=protocol.load_ledger(protocol.REQUIREMENTS_PATH)
    intended=len(catalog.get('requirements',[])) if catalog is not None and not catalog_error else None
    report=protocol.evaluate(ledger,'release',expected_source_sha=candidate.source_sha,
        expected_image_digest=candidate.image_digest,expected_cut_at=candidate.cut_at,
        expected_component_source_shas=dict(candidate.component_source_shas),
        expected_client_versions=dict(candidate.client_versions),now=now,
        receipt_root=receipt_root)
    return {'status':'ready-for-normal-plan' if report['overall_status']=='pass' else 'unqualified',
            'intendedCatalogCells':intended,'localClientReport':report,'candidateQualified':False,'sixAwsCellsQualified':False,
            'terminal123Qualified':False}


def provision_request(*, action: str, variables_json: str, idempotency_key: str,
                      operation_id: str | None=None, approval_original: str=''):
    """Build only the existing fixed aws-ecs/small public MCP request.

    This does not validate/issue an approval or grant any execution tier. The
    production tool must authenticate its normal exact signed receipt and claim
    before mutation. The same original variables are retained across plan/apply.
    """
    require(action in ('plan','apply','destroy-plan','destroy','inspect-cleanup'), 'provision-action')
    require(type(variables_json) is str and 0<len(variables_json)<=1024*1024,'provision-variables-bound')
    require(type(idempotency_key) is str and re.fullmatch(r'[A-Za-z0-9._:-]{1,160}',idempotency_key),
            'provision-idempotency-key')
    mutating=action in ('apply','destroy')
    if mutating or action=='inspect-cleanup':
        require(type(operation_id) is str and re.fullmatch(r'urn:honua:provisioning:[0-9a-f]{32}',operation_id),
                'provision-operation-identity')
    if mutating:
        require(type(approval_original) is str and bool(approval_original),'provision-approval-missing')
    else: require(approval_original=='','provision-approval-unrequested')
    # Environment is supplied by the authenticated structured approvalRequest,
    # not extracted from summary prose or a generic candidate reference.
    return {'name':'provision_infrastructure','arguments':{
        'stack':'aws-ecs','size':'small','action':'destroy' if action=='destroy-plan' else action,
        'variablesJson':variables_json,'confirmed':mutating,'confirmation':'',
        'approvalReceiptJson':approval_original,'idempotencyKey':idempotency_key,
        'provisioningOperationId':operation_id or ''}}


def bind_confirmation(request: dict, approval_request: dict):
    """Use actual production structured projection to bind the existing challenge."""
    require(type(request) is dict and request.get('name')=='provision_infrastructure'
            and type(request.get('arguments')) is dict,'provision-fixed-request')
    args=request['arguments']
    require(args.get('confirmed') is True and args.get('action') in ('apply','destroy'),
            'provision-confirmation-phase')
    require(type(approval_request) is dict and approval_request.get('action')==args['action']
            and approval_request.get('stack')=='aws-ecs'
            and approval_request.get('provisioningOperationId')==args.get('provisioningOperationId'),
            'provision-approval-projection')
    environment=approval_request.get('environment')
    require(environment in ('dev','staging','it'),'provision-environment')
    result={'name':request['name'],'arguments':dict(args)}
    token=args['provisioningOperationId'].removeprefix('urn:honua:provisioning:')
    result['arguments']['confirmation']=args['action']+':aws-ecs:'+environment+':'+token
    return result


def require_six_cells(cells: Mapping[str,object]):
    require(set(cells)==set(CELLS),'cleanup-exact-six-cell-closure')
    return tuple(CELLS)
