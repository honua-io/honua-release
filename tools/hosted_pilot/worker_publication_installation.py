"""Checked installer transport for original worker publication bytes.

Only the Actions ZIP download uses the install job's existing GitHub credential.
Consumers replay the retained ZIP through the public-metadata authenticator.
No image build, credential write, resource apply or activation is performed here.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixture_handoff import require
from human_memberships import github_json
from worker_runtime_publication import (INDEX_FILE, LIMIT, REPOSITORY,
    publication_originals, validate_binding, verify_publication)

ARCHIVE_NAME = 'worker-publication.zip'


def bounded_output(command, *, guard, timeout=60, limit=LIMIT):
    """Bound subprocess time and stdout without buffering an unbounded ZIP.

    stderr is deliberately discarded: gh errors can include signed download
    URLs. Failures expose only a stable transport refusal, never that URL/token.
    """
    guard()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    deadline = time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline-time.monotonic()
                require(remaining > 0, 'worker publication download exceeded its deadline')
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, limit+1-len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    require(len(output) <= limit, 'worker publication download exceeded its byte bound')
            require(deadline > time.monotonic(), 'worker publication download exceeded its deadline')
            try:
                status = process.wait(timeout=deadline-time.monotonic())
            except subprocess.TimeoutExpired:
                require(False, 'worker publication download exceeded its deadline')
            require(status == 0,
                'worker publication original download failed')
        guard()
        return bytes(output)
    finally:
        # A failed/oversized download must not leave a redirect or gh child live.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def download_archive(binding, *, guard):
    validate_binding(binding)
    return bounded_output(['gh', 'api', '--method', 'GET',
        'repos/'+REPOSITORY+'/actions/artifacts/'+str(binding['artifactId'])+'/zip'], guard=guard)


def authenticate(configuration, archive, *, index_raw=None, github=github_json, guard):
    require(configuration.get('installed_slot') is not None,
        'worker publication is an installed-slot prerequisite')
    return verify_publication(configuration.get('worker_publication'), archive=archive,
        index_raw=index_raw, image=configuration['worker_image'],
        public_bundle_digest=configuration['public_bundle_digest'], github=github, guard=guard)


def _save_original(path, raw):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        require(path.is_file() and not path.is_symlink() and path.stat().st_size == len(raw) and
            path.read_bytes() == raw, 'worker publication private original already differs')
    else:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)


def prepare(configuration, work, *, guard, github=github_json, download=download_archive):
    """Called before Terraform plan/apply; static campaign behavior is unchanged."""
    if configuration.get('installed_slot') is None:
        return None
    validate_binding(configuration.get('worker_publication'))
    archive = download(configuration['worker_publication'], guard=guard)
    verified = authenticate(configuration, archive, github=github, guard=guard)
    originals = publication_originals(archive)
    _save_original(Path(work)/ARCHIVE_NAME, archive)
    _save_original(Path(work)/INDEX_FILE, originals[INDEX_FILE])
    guard()
    return verified


def saved(configuration, work, *, github=github_json, guard):
    """Reauthenticate the exact private bytes before retaining installation proof."""
    values = []
    for name in (ARCHIVE_NAME, INDEX_FILE):
        path = Path(work)/name
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= LIMIT,
            'worker publication private originals are missing or oversized')
        with path.open('rb') as stream:
            raw = stream.read(LIMIT+1)
        require(len(raw) <= LIMIT, 'worker publication private original grew beyond its bound')
        values.append(raw)
    verified = authenticate(configuration, values[0], index_raw=values[1], github=github, guard=guard)
    return verified, values[0], values[1]


def verify_retained(binding, *, configuration, prefix, bucket, read_bytes,
                    github=github_json, guard):
    """Return proof.runtime only after replaying both original immutable objects."""
    require(isinstance(binding, dict) and set(binding) == {'binding', 'publication', 'indexReference'} and
        binding['binding'] == configuration.get('worker_publication'),
        'installed publication binding differs from original checked configuration')
    for field, filename in (('publication', ARCHIVE_NAME), ('indexReference', INDEX_FILE)):
        reference = binding[field]
        require(isinstance(reference, dict) and reference.get('bucket') == bucket and
            reference.get('key') == prefix+filename,
            'installed publication escaped original worker output custody')
    archive = read_bytes(binding['publication'], guard=guard)
    index_raw = read_bytes(binding['indexReference'], guard=guard)
    verified = authenticate(configuration, archive, index_raw=index_raw, github=github, guard=guard)
    return {'image': configuration['worker_image'], 'publication': binding['publication'],
        'indexReference': binding['indexReference'], 'index': verified['index'],
        'runtimeBundleDigest': verified['metadata']['runtimeBundleDigest']}
