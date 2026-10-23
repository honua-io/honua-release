"""Immutable custody completion metadata, distinct from the original creator."""
import json
import re

from deploy import digest, private_json, require


def prefix(configuration, creator):
    require(re.fullmatch(r'[0-9]+', creator['run_id']), 'original custody creator run missing')
    return f'honua/hosted-pilot/{configuration["name_prefix"]}/foundation-recovery/{creator["run_id"]}/completions/'


def retain(receipt, configuration, binding, work, *, aws, exists):
    path = work/'custody-applied.json'
    require(json.loads(path.read_text()) == receipt, 'custody completion differs from its original file')
    key = prefix(configuration, receipt['original_creation']) + digest(path) + '.json'
    if not exists('head-object', '--bucket', binding['bucket'], '--key', key):
        result = aws('s3api', 'put-object', '--bucket', binding['bucket'], '--key', key,
            '--body', str(path), '--server-side-encryption', 'aws:kms', '--ssekms-key-id', binding['kms_key_id'], '--if-none-match', '*')
        require(result.get('VersionId') not in (None, 'null'), 'custody completion did not retain an immutable version')
    result = aws('s3api', 'head-object', '--bucket', binding['bucket'], '--key', key)
    require(result.get('VersionId') not in (None, 'null') and result.get('ServerSideEncryption') == 'aws:kms' and
        result.get('SSEKMSKeyId') == binding['kms_key_id'], 'custody completion immutable encryption/version differs')
    observed = work/'retained-custody-completion-readback.json'
    readback = aws('s3api', 'get-object', '--bucket', binding['bucket'], '--key', key, '--version-id', result['VersionId'], str(observed))
    require(readback.get('VersionId') == result['VersionId'] and digest(observed) == digest(path), 'durable custody completion bytes differ')
    reference = {'bucket': binding['bucket'], 'key': key, 'version_id': result['VersionId'], 'sha256': digest(path)}
    private_json(work/'custody-completion-storage.json', reference)
    return reference


def observe_successful_recovery(configuration, binding, original, work, *, aws, call=None):
    """Authenticate native recovery approval/apply; never turn its creator green."""
    import history_reconciliation as history
    call = history.invoke if call is None else call
    base = prefix(configuration, original)
    token = None; seen_tokens = set(); objects = []
    for _ in range(10):
        args = ['s3api', 'list-objects-v2', '--bucket', binding['bucket'], '--prefix', base]
        if token is not None: args += ['--continuation-token', token]
        page = aws(*args)
        objects += page.get('Contents', [])
        if page.get('IsTruncated') is False:
            break
        token = page.get('NextContinuationToken')
        require(isinstance(token, str) and token and token not in seen_tokens, 'custody completion inventory incomplete')
        seen_tokens.add(token)
    else:
        raise ValueError('custody completion inventory exceeds fixed bound')
    successes = []; refused = []
    require(len({row['Key'] for row in objects}) == len(objects), 'duplicate custody completion metadata')
    for index, row in enumerate(objects):
        key = row['Key']
        require(re.fullmatch(re.escape(base)+r'[a-f0-9]{64}\.json', key), 'unrecognized original custody completion key')
        metadata = aws('s3api', 'head-object', '--bucket', binding['bucket'], '--key', key)
        version = metadata.get('VersionId')
        require(version not in (None, 'null') and 0 < metadata.get('ContentLength', 0) <= 4*1024*1024 and
            metadata.get('ServerSideEncryption') == 'aws:kms' and metadata.get('SSEKMSKeyId') == binding['kms_key_id'],
            'custody completion lacks bounded immutable content')
        path = work/f'observed-custody-completion-{index}.json'
        fetched = aws('s3api', 'get-object', '--bucket', binding['bucket'], '--key', key, '--version-id', version, str(path))
        require(fetched.get('VersionId') == version and digest(path) == key[len(base):-5], 'custody completion version or content address differs')
        value = json.loads(path.read_text())
        require(value.get('schema') == 'honua.hosted-pilot-custody-applied/v1' and value.get('original_creation') == original,
            'custody completion substitutes its original failed creator')
        review = value['review']
        producer = {'repository': 'honua-io/honua-iac', 'workflow': '.github/workflows/hosted-pilot-bootstrap.yml',
            'runId': int(review['run_id']), 'runAttempt': int(review['run_attempt']), 'sourceSha': review['foundation_source_sha']}
        require(producer['runId'] > 0 and producer['runAttempt'] > 0 and re.fullmatch(r'[a-f0-9]{40}', producer['sourceSha']), 'invalid recovery producer')
        run = history.api(f'repos/honua-io/honua-iac/actions/runs/{producer["runId"]}/attempts/{producer["runAttempt"]}', call)
        require(run.get('id') == producer['runId'] and run.get('run_attempt') == producer['runAttempt'] and
            run.get('head_sha') == producer['sourceSha'] and run.get('path') == producer['workflow'] and
            run.get('event') == 'workflow_dispatch' and run.get('head_branch') == 'trunk', 'recovery native source/run differs')
        reference = {'bucket': binding['bucket'], 'key': key, 'version_id': version, 'sha256': digest(path)}
        if run.get('status') == 'completed' and run.get('conclusion') in ('failure', 'cancelled', 'timed_out'):
            refused.append({'producer': producer, 'conclusion': run['conclusion'], 'reference': reference})
            continue
        require(run.get('status') == 'completed' and run.get('conclusion') == 'success', 'recovery is not positively complete')
        listing = history.api(f'repos/honua-io/honua-iac/actions/runs/{producer["runId"]}/artifacts?per_page=100', call)
        require(len(listing['artifacts']) == listing['total_count'], 'original recovery artifacts truncated')
        def artifact(name):
            selected = [item for item in listing['artifacts'] if item['name'] == name]
            require(len(selected) == 1 and re.fullmatch(r'sha256:[a-f0-9]{64}', selected[0].get('digest', '')), 'original recovery artifact missing or ambiguous')
            return {'id': selected[0]['id'], 'name': name, 'sha256': selected[0]['digest']}
        record = {'kind': 'foundation-custody', 'producer': producer, 'ownerLedger': None, 'cloudEvents': [],
            'artifact': artifact(f'hosted-pilot-custody-applied-{producer["runId"]}-{producer["runAttempt"]}'),
            'planArtifact': artifact(f'hosted-pilot-phase-review-{producer["runId"]}-{producer["runAttempt"]}-custody-only')}
        native = history.native_artifact(producer, record['artifact'], 'custody-applied.json', call)
        require(native == value, 'durable completion differs from its native original')
        # This independently verifies protected producer sources and the exact
        # original review, real human approval, admission and completed apply.
        history.authenticate(record, call=call)
        successes.append({'producer': producer, 'reference': reference, 'original_artifact': record['artifact']})
    require(successes, 'original creator is unsuccessful; authenticated successful custody recovery required')
    chosen = max(successes, key=lambda item: (item['producer']['runId'], item['producer']['runAttempt']))
    result = {'schema': 'honua.observed-custody-recovery/v1', 'original_creation': original,
        'successful_recovery': chosen, 'retained_unsuccessful_completions': refused, 'financial_settlement': False, 'qualification': False}
    private_json(work/'original-successful-custody.json', result)
    return result
