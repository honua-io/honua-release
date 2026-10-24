"""Retain one observed cancelled custody run without admitting an apply or reset."""
from decimal import Decimal
import hashlib
import json

from controller_archive import require
from history_phase import original_review, step
from bootstrap_phase import stamp

KIND = 'foundation-expired-before-apply'
PRODUCER = {'repository': 'honua-io/honua-iac', 'runId': 40039656211, 'runAttempt': 1,
            'sourceSha': '35ae5bcb5153fd4925be12ad28abbece78a1acac',
            'workflow': '.github/workflows/hosted-pilot-bootstrap.yml'}
RELEASE_SHA = 'a1eec7b99fbdbe65b1882a431054d6e658f5f1e0'
CONFIG_PATH = 'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json'
CONFIG_SHA256 = 'ebb42c3be45e9efa7bec217628ac006cc43106f9172630c5d9b62691e90e5f09'
FUNDING_BASIS = {'path': 'certification/hosted-pilot/foundation-funding-basis.json',
                 'sha256': 'sha256:81a1740a73175caef641c78ba800538ca4ec7726e4a689272850ecc9a5a718e4'}
RECOVERY = {'id': 12539034731, 'name': 'hosted-pilot-custody-recovery-40039656211',
            'sha256': 'sha256:7e5dff14eb18146c41c594f57519010fc9b48b13843405bd06f179b66db34e2c'}
REVIEW = {'id': 12538662141, 'name': 'hosted-pilot-phase-review-40039656211-1-custody-only',
          'sha256': 'sha256:d43bc9440aace3c494990e09a3f6a216e8601fcdac0fe85fe2fbafe679fac5cb'}
JOBS = {'approval-policy': (120707468264, 'success'), 'bootstrap': (120707571202, 'cancelled'),
        'review-ready': (120707571268, 'success'), 'phase-watchdog': (120707571348, 'cancelled'),
        'phase-approval': (120709091054, 'cancelled')}
IAC_SOURCES = {'.github/workflows/hosted-pilot-bootstrap.yml': 'abf38a49f3de66de221f300cc78ae8cdb8198f9e81a87276146d502361a2a5c5',
 'scripts/check-hosted-pilot-foundation-inputs.py': '9728d194f962cbbfcbba89cde20997e97af404c602f5c0fa5346e0d3bc06ba57'}
RELEASE_SOURCES = {'certification/hosted-pilot/bootstrap/.terraform.lock.hcl': '7fd30e70f88ed9c5de18a5d13a5d91303fe233a14719dede9d6ededd3a2d0851',
 'certification/hosted-pilot/bootstrap/agent-boundary.tf': '573b4238a990ca055118eff9a45b84fe6c993b622a6aed8d99b49e70478207d7',
 'certification/hosted-pilot/bootstrap/controller-image.tf': 'f3cae720c0ab2a7f4f68af55b41493d8dc8460a2fc3257ac48486c938ee210f3',
 'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json': 'ebb42c3be45e9efa7bec217628ac006cc43106f9172630c5d9b62691e90e5f09',
 'certification/hosted-pilot/bootstrap/human-enrollment.tf': 'fd69a45d304562927e69144cb598a7b97fec52cbcfedf27bf35899bef43642e8',
 'certification/hosted-pilot/bootstrap/main.tf': 'da43cebc903fbfde62ea7614a08dc75f006caf9b6db3c425d25e40109e325fa1',
 'certification/hosted-pilot/bootstrap/native-bootstrap-boundary.tf': '35904108da194edf436d2c3937d22cff30b53c97db8d8ae288b60a490ddcaa45',
 'certification/hosted-pilot/bootstrap/outputs.tf': '4cbf86842cbcbb502e92c8c43352e695a7a3e3342ea61e14e1ca4df82b90c173',
 'certification/hosted-pilot/bootstrap/variables.tf': 'f3c94a6f438db836889fe80458b77180a49c0571e56a485c5e7be8c88a667e40',
 'certification/hosted-pilot/bootstrap/versions.tf': '7e83fb16578c7c8379d3656869cb478bce596701eeccfaa0ce297ee1b0b161f5',
 'certification/hosted-pilot/bootstrap/worker-budget-read.tf': 'c2faa4045d384ba7f66ed1c9e7ad3a66c2393ac6c2879cbc91ccf18a72f5ca76',
 'certification/hosted-pilot/deployments/foss4g-image-preparation.tfvars.json': 'febee448868ad517cb9ae132f7a256eeca6a23ee0e1c4277bf76cddb59c24ee1',
 'certification/hosted-pilot/foundation-environment.json': 'a2aa5e4a94dc5a9fc6168aea18e213fb1f28e416f025ff8a66f8932bf875087b',
 'certification/hosted-pilot/foundation-funding-basis.json': '81a1740a73175caef641c78ba800538ca4ec7726e4a689272850ecc9a5a718e4',
 'tools/hosted_pilot/bind_github.py': 'a9938c4c6ce36c287885c049cf0eb15bf862a016eb961ec7bfe06f95e97c31f6',
 'tools/hosted_pilot/bootstrap.py': '5115193b5db84006ef03e89f56491a2faf196d921e58d1059adfd153f4b3f357',
 'tools/hosted_pilot/bootstrap_phase.py': '8c38b8c77f4886845a76aee6b0ce33625f910134bc20c88d42f44f2da9377669',
 'tools/hosted_pilot/custody_completion.py': 'c64bfbbbccf725d8efe4e33659464ce6bd41592cb1381d144d6c07f432d7b385',
 'tools/hosted_pilot/deploy.py': '0fd9add4c1af2ce97caadeb997b79eabc71d735c63c23f449801c5f5ddaba475',
 'tools/hosted_pilot/foundation_environment.py': 'ff4ff20957736f36cc0cfd1573bf6bc86bef4dbb4cc8d19602d0dafcd7014ddb'}


def validate_funding_rows(record, rows, ledger):
    """Original amounts remain; authentication never invents settlement or IDs."""
    require(record['fundingBasis'] == FUNDING_BASIS, 'expired phase lost its original334 funding basis')
    totals = {kind: sum(Decimal(row['amountUsd']) for row in rows if row['kind'] == kind)
              for kind in ('infrastructure', 'cleanup')}
    require(totals == {'infrastructure': Decimal(25), 'cleanup': Decimal(10)},
            'expired custody must retain original25 infrastructure and10 cleanup amounts')
    require(sum(Decimal(row['amountUsd']) for row in ledger['operations']) >= Decimal(334),
            'expired source history cannot discard the original334 liability amounts')


def authenticate(record, call):
    import history_reconciliation as h
    producer = record['producer']
    require(record['kind'] == KIND and producer == PRODUCER,
            'only the actually observed expired custody run may use this classification')
    require(record['ownerLedger'] is None and not record['cloudEvents'] and
            record['artifact'] == RECOVERY and record['planArtifact'] == REVIEW and
            record['fundingBasis'] == FUNDING_BASIS,
            'expired phase cannot claim ownership events, publication or substituted originals')
    base = 'repos/' + producer['repository']
    run = h.api(base + '/actions/runs/40039656211/attempts/1', call)
    require((run['id'], run['run_attempt'], run['head_sha'], run['path'], run['head_branch'],
             run['event'], run['status'], run['conclusion']) ==
            (40039656211, 1, producer['sourceSha'], producer['workflow'], 'trunk',
             'workflow_dispatch', 'completed', 'cancelled'),
            'expired custody must retain its actual cancelled original outcome')
    for repository, source in [(producer['repository'], producer['sourceSha']), ('honua-io/honua-release', RELEASE_SHA)]:
        root = 'repos/' + repository
        require(h.api(root + '/branches/trunk', call)['protected'] is True, 'original source is not protected')
        require(h.api(root + '/compare/' + source + '...trunk', call)['merge_base_commit']['sha'] == source,
                'original source left protected trunk')
    jobs = h.api(base + '/actions/runs/40039656211/attempts/1/jobs?per_page=100', call)
    require(len(jobs['jobs']) == jobs['total_count'] == len(JOBS) and
            {row['name']: (row['id'], row['conclusion']) for row in jobs['jobs']} == JOBS and
            all(row['status'] == 'completed' and row['head_sha'] == producer['sourceSha'] and
                row['run_id'] == producer['runId'] for row in jobs['jobs']),
            'expired original job inventory/source/outcome changed')
    expected = {
        'Refuse unreviewed or floating release root': 'success',
        'Verify exact candidate-independent configuration and release compiler before AWS credentials': 'success',
        'Prepare exactly one source-reviewed phase without applying': 'success',
        'Publish value-free review before waiting for native approval': 'success',
        'Wait for original exact-plan approval from the separate protected job': 'failure',
        'Apply only the exact reviewed phase and stop': 'skipped',
        'Preserve initial-custody recovery identities after failure or cancellation': 'success',
        'Retain only custody resource-ID recovery journal': 'success',
        'Retain only allowlisted nonsecret deployment bindings': 'skipped',
        'Retain only exact custody-phase completion metadata': 'skipped',
        'Remove private executor material': 'success',
    }
    for name, conclusion in expected.items():
        row = step(jobs, name)
        require(row['status'] == 'completed' and row['conclusion'] == conclusion,
                'expired original phase lacks positive skipped apply and retained cleanup')
    require(h.api(base + '/actions/runs/40039656211/approvals', call) == [],
            'expired original gained a human approval record')
    artifacts = h.api(base + '/actions/runs/40039656211/artifacts?per_page=100', call)
    require(len(artifacts['artifacts']) == artifacts['total_count'] == 2 and
            {(row['id'], row['name']) for row in artifacts['artifacts']} ==
            {(RECOVERY['id'], RECOVERY['name']), (REVIEW['id'], REVIEW['name'])},
            'expired original has an unknown or applied/admitted artifact')
    recovery = h.native_artifact(producer, RECOVERY, 'custody-recovery.json', call)
    review = h.native_artifact(producer, REVIEW, 'review.json', call)
    original_review(record, review, jobs)
    require(review['release_root_sha'] == RELEASE_SHA and review['phase'] == 'custody-only' and
            review['config_path'] == CONFIG_PATH and review['config_sha256'] == CONFIG_SHA256 and
            review['provider_lock_sha256'] == RELEASE_SOURCES['certification/hosted-pilot/bootstrap/.terraform.lock.hcl'] and
            review['original_creation']['run_id'] == str(producer['runId']),
            'expired original review source, phase or configuration changed')
    wait = step(jobs, 'Wait for original exact-plan approval from the separate protected job')
    require(stamp(wait['completed_at']) >= stamp(review['approval_expires_at']) and
            all(stamp(row['completed_at']) < stamp(review['execution_expires_at']) for row in jobs['jobs']),
            'cancelled phase does not retain its original expired bounded window')
    actor = review['execution_identity']
    require(actor['run_id'] == str(producer['runId']) and actor['run_attempt'] == '1' and
            actor['foundation_source_sha'] == producer['sourceSha'] and actor['release_root_sha'] == RELEASE_SHA and
            actor['account_id'] == '585192672263' and actor['run_unique_session'] is False,
            'expired phase lost original execution identity or attribution limitation')
    require(recovery['schema'] == 'honua.hosted-pilot-custody-recovery/v1' and
            recovery['foundation_source_sha'] == producer['sourceSha'] and recovery['release_root_sha'] == RELEASE_SHA and
            recovery['bootstrap_run_id'] == str(producer['runId']) and recovery['config_path'] == CONFIG_PATH and
            recovery['config_sha256'] == CONFIG_SHA256 and recovery['provider_lock_sha256'] == review['provider_lock_sha256'] and
            recovery['cloud_discovery_complete'] is True and recovery['resources'] == [] and
            recovery['remote_recovery_state'] is None and recovery['unmapped_owned_key_ids'] == [] and
            recovery['unverified_bucket_names'] == [],
            'expired bounded discovery changed; no zero-spend or ownership inference is admitted')
    h.verify_sources(producer['repository'], producer['sourceSha'], IAC_SOURCES, call)
    h.verify_sources('honua-io/honua-release', RELEASE_SHA, RELEASE_SOURCES, call)
    config = h.source_file(producer['repository'], producer['sourceSha'], CONFIG_PATH, call)
    require(hashlib.sha256(config).hexdigest() == CONFIG_SHA256, 'original expired phase input changed')
    return run, jobs, recovery, RELEASE_SHA


PLAN_PRODUCER = dict(PRODUCER, runId=40038616357)
PLAN_ARTIFACT = {'id': 12537724933, 'name': 'hosted-pilot-phase-review-40038616357-1-custody-only',
                 'sha256': 'sha256:bcaaeccf1add0344dc9fb5dd2cd1b16218751523cca5bd29f228e463f60454a4'}


def observed_plan_sources(record, receipt):
    """Keep the actually observed successful plan valid after helper evolution."""
    if record['producer'] != PLAN_PRODUCER:
        return None
    require(record['kind'] == 'foundation-plan' and record['artifact'] == PLAN_ARTIFACT and
            receipt['release_root_sha'] == RELEASE_SHA and receipt['phase'] == 'custody-only' and
            receipt['plan_sha256'] == '31316ca4cd951b061510d80c200bbceb41d3cc98d99867f33203aaa24657f4af',
            'observed successful phase plan or artifact changed')
    # All native success, actor, skipped-apply, immutable artifact and funding
    # checks still execute in the ordinary successful-plan authenticator.
    return IAC_SOURCES, RELEASE_SOURCES
