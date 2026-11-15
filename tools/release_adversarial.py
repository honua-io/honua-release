"""Sanitized white-hat and chaos receipt checks; never publish report contents."""
import hashlib
import io
import re
import zipfile

IDS = frozenset({'security-whitehat', 'chaos'})
MODELS = frozenset({'claude-fable-5-2', 'gpt-6-astra'})
FAULTS = frozenset({'stop-tasks', 'wedge-task', 'stop-database', 'fill-database',
                    'crashing-release', 'error-release', 'block-probe', 'stop-watchdog',
                    'flood-requests'})


def attach_reports(doc, blob):
    """Read only declared sanitized reports from the authenticated Actions artifact."""
    doc.pop('_archivedReports', None)
    verified = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for review in doc.get('reviews', []):
                name = review.get('report')
                if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9-]+\.md', name):
                    continue
                files = [f for f in archive.infolist() if f.filename == name]
                if len(files) == 1 and 0 < files[0].file_size <= 100_000:
                    digest = hashlib.sha256(archive.read(files[0])).hexdigest()
                    if review.get('reportSha256') == digest:
                        verified.append(name)
    except (zipfile.BadZipFile, ValueError, OSError, AttributeError, TypeError):
        pass
    doc['_archivedReports'] = verified


def valid(doc):
    if doc.get('row') == 'chaos':
        faults = doc.get('faults')
        return (isinstance(faults, list) and len(faults) == len(FAULTS)
                and all(isinstance(f, dict) for f in faults)
                and {f.get('id') for f in faults} == FAULTS
                and all(isinstance(f.get(k), dict) and f[k].get('status') == 'PASS'
                        and isinstance(f[k].get('evidence'), str) and f[k]['evidence'].strip()
                        for f in faults for k in ('telemetry', 'logging', 'remediation')))
    reviews = doc.get('reviews')
    if (doc.get('stackName') != 'honua-gate-whitehat'
            and not re.fullmatch(r'honua-gate-whitehat-[a-zA-Z0-9-]+', str(doc.get('stackName', '')))):
        return False
    if not isinstance(doc.get('stackId'), str) or not doc['stackId'].strip():
        return False
    if any(type(doc.get(k)) is not int or doc[k] != 0
           for k in ('openNewHighFindings', 'openNewCriticalFindings')):
        return False
    if not isinstance(reviews, list) or len(reviews) != 2 or any(not isinstance(r, dict) for r in reviews):
        return False
    return ({r.get('model') for r in reviews} == MODELS
            and len({r.get('reviewer') for r in reviews if isinstance(r.get('reviewer'), str)
                     and r['reviewer'].strip()}) == 2
            and len({r.get('report') for r in reviews}) == 2
            and all(r.get('source') is True and r.get('live') is True
                    and r.get('status') == 'PASS' and r.get('report') in doc.get('_archivedReports', [])
                    for r in reviews))
