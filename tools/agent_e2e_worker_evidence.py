"""Controller-owned, create-only storage for bounded untrusted MCP reply chunks.

This is the retention callback for Worker.execute_mcp, not a provisioner, dispatch
authorizer, external-action fence or qualification oracle. Costs must already be
reserved. Recovery never sends an agent command or constructs a success receipt.
"""
from __future__ import annotations

import hashlib
import base64
import json
import re

from agent_e2e_worker import MCP_PROTOCOL, ReplyChunks, PackError, encode, CHUNK_BYTES, MAX_REPLY_BYTES


def digest(raw):
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


def open_evidence(config, candidate_key, attempt_id, action_id):
    if (not isinstance(config, dict) or set(config) != {'region','bucket'}
            or not all(isinstance(value, str) and value.strip() for value in config.values())):
        raise PackError('invalid worker evidence configuration')
    import boto3
    from botocore.config import Config
    s3 = boto3.client('s3', region_name=config['region'],
        config=Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts':1}))
    return ReplyEvidence(s3, config['bucket'], candidate_key, attempt_id, action_id)


class ReplyEvidence:
    def __init__(self, s3, bucket, candidate_key, attempt_id, action_id):
        if (not isinstance(candidate_key, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', candidate_key)
                or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', value)
                       for value in (attempt_id, action_id))):
            raise PackError('invalid worker evidence identity')
        self.s3, self.bucket = s3, bucket
        self.identity = {'candidateKey':candidate_key, 'attemptId':attempt_id, 'actionId':action_id}
        self.prefix = f"evidence/{candidate_key[7:]}/worker/{attempt_id}/{action_id}/"
        self.dispatch_key = f"evidence/{candidate_key[7:]}/worker-dispatch/{attempt_id}/{action_id}.json"
        if s3.get_bucket_versioning(Bucket=bucket).get('Status') != 'Enabled':
            raise PackError('worker evidence requires versioned controller storage')
        self.header, self.header_reference, self.collector = None, None, None
        self.references = {}
        self.expected_binding = None
        self.dispatch_reference = None

    def retain_dispatch(self, binding):
        """Synchronously retain exact command identity before any queue send."""
        expected = ReplyChunks.from_binding(binding).expected
        if any(expected[key] != self.identity[key] for key in ('attemptId', 'actionId')):
            raise PackError('worker dispatch belongs to another action')
        value = {'schemaVersion':1, **self.identity, 'binding':expected}
        reference = self._put(self.dispatch_key, value)
        self.expected_binding, self.dispatch_reference = expected, reference
        return reference

    def load_dispatch(self):
        """Load controller-retained identity; it is not permission to dispatch."""
        value, reference = self._discover(self.dispatch_key)
        if (not isinstance(value, dict) or set(value) != {'schemaVersion', *self.identity, 'binding'}
                or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1
                or any(value[key] != expected for key, expected in self.identity.items())):
            raise PackError('invalid retained worker dispatch')
        expected = ReplyChunks.from_binding(value['binding']).expected
        if any(expected[key] != self.identity[key] for key in ('attemptId', 'actionId')):
            raise PackError('retained worker dispatch belongs to another action')
        return {'binding':expected, 'reference':reference}

    def _read(self, key, version, expected_digest=None):
        if not isinstance(version, str) or not version or version == 'null':
            raise PackError('worker evidence object lacks a retained version')
        response = self.s3.get_object(Bucket=self.bucket, Key=key, VersionId=version)
        if response.get('VersionId') != version:
            raise PackError('worker evidence returned another object version')
        with response['Body'] as body:
            raw = body.read(60001)
        if len(raw) > 60000 or expected_digest is not None and digest(raw) != expected_digest:
            raise PackError('oversized or changed worker evidence object')
        value = json.loads(raw)
        if encode(value).encode() != raw:
            raise PackError('worker evidence object is not canonical JSON')
        return value, {'key':key, 'version':version, 'sha256':digest(raw)}

    def _discover(self, key, *, allow_missing=False):
        # If publication acknowledgement was lost, adopt only one retained exact
        # version. Missing, deleted, truncated or conflicting history blocks.
        result = self.s3.list_object_versions(Bucket=self.bucket, Prefix=key, MaxKeys=2)
        versions = result.get('Versions', [])
        if allow_missing and not versions and not result.get('DeleteMarkers') and not result.get('IsTruncated'):
            return None
        if (result.get('IsTruncated') or result.get('DeleteMarkers') or len(versions) != 1
                or versions[0].get('Key') != key or versions[0].get('IsLatest') is not True):
            raise PackError('worker evidence history is missing or ambiguous; reconcile without redispatch')
        return self._read(key, versions[0].get('VersionId'))

    def _put(self, key, value):
        raw = encode(value).encode()
        if len(raw) > 60000:
            raise PackError('oversized worker evidence object')
        checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        try:
            response = self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw,
                ContentType='application/json', IfNoneMatch='*', ChecksumSHA256=checksum)
        except Exception as exc:
            # Only a definite create-only conflict permits reading an existing
            # object. Timeouts/connection failures remain unacknowledged.
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') not in ('PreconditionFailed', '412'):
                raise
            existing, reference = self._discover(key)
            if existing != value or reference['sha256'] != digest(raw):
                raise PackError('worker evidence cannot replace retained content') from exc
            return reference
        version = response.get('VersionId')
        if (not isinstance(version, str) or not version or version == 'null'
                or response.get('ChecksumSHA256') != checksum):
            raise PackError('worker evidence write lacks verified version/checksum acknowledgement')
        # S3 permits If-None-Match writes over a current delete marker. A PUT
        # acknowledgement alone therefore does not establish recoverable history.
        existing, reference = self._discover(key)
        if (reference['version'] != version or reference['sha256'] != digest(raw)
                or existing != value):
            raise PackError('worker evidence write and retained history disagree')
        return reference

    def _validate_header(self, value):
        if (not isinstance(value, dict) or set(value) != {'schemaVersion', *self.identity, 'binding', 'payloadDigest', 'byteLength', 'count'}
                or value['schemaVersion'] != 1 or type(value['schemaVersion']) is not int
                or any(value.get(key) != expected for key, expected in self.identity.items())
                or type(value['byteLength']) is not int or not 0 < value['byteLength'] <= MAX_REPLY_BYTES
                or type(value['count']) is not int or value['count'] != (value['byteLength']+CHUNK_BYTES-1)//CHUNK_BYTES
                or not isinstance(value['payloadDigest'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['payloadDigest'])):
            raise PackError('invalid worker evidence header')
        collector = ReplyChunks.from_binding(value['binding'])
        if any(value['binding'][key] != self.identity[key] for key in ('attemptId','actionId')):
            raise PackError('worker evidence header belongs to another action')
        collector.binding = (value['payloadDigest'], value['byteLength'], value['count'])
        return collector

    def __call__(self, chunk):
        if self.header is None:
            if not isinstance(chunk, dict):
                raise PackError('invalid worker evidence chunk')
            binding = {'protocol':MCP_PROTOCOL, **{key:chunk.get(key) for key in ('attemptId','actionId','commandDigest')}}
            if self.expected_binding is not None and binding != self.expected_binding:
                raise PackError('worker evidence chunk differs from retained dispatch binding')
            header = {'schemaVersion':1, **self.identity, 'binding':binding,
                      **{key:chunk.get(key) for key in ('payloadDigest','byteLength','count')}}
            collector = self._validate_header(header)
            collector.accept(chunk)  # Validate before any storage mutation.
            reference = self._put(self.prefix+'header.json', header)
            self.header, self.header_reference, self.collector = header, reference, collector
        else:
            self.collector.accept(chunk)
        reference = self._put(self.prefix+f"chunk-{chunk['index']:03d}.json", chunk)
        self.references[chunk['index']] = reference
        return reference

    def resume(self, binding):
        """Load retained partial output before receiving any more queue messages.

        The caller obtains binding from trusted dispatch history, never the worker
        or first queue message. This grants neither dispatch nor qualification.
        """
        expected = ReplyChunks.from_binding(binding).expected
        if any(expected[key] != self.identity[key] for key in ('attemptId', 'actionId')):
            raise PackError('worker evidence recovery belongs to another action')
        found = self._discover(self.prefix+'header.json', allow_missing=True)
        if found is None:
            # No header is valid only before the first retained chunk. Orphans
            # imply lost history; receiving new output must not conceal that.
            history = self.s3.list_object_versions(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
            if history.get('Versions') or history.get('DeleteMarkers') or history.get('IsTruncated'):
                raise PackError('worker evidence header missing with existing history')
            self.header, self.header_reference, self.collector = None, None, None
            self.references, self.expected_binding = {}, expected
            return None
        header, reference = found
        collector = self._validate_header(header)
        if collector.expected != expected:
            raise PackError('worker evidence header differs from retained dispatch binding')
        references, reply = {}, None
        for index in range(header['count']):
            found_part = self._discover(self.prefix+f'chunk-{index:03d}.json', allow_missing=True)
            if found_part is None:
                continue
            chunk, part_reference = found_part
            if chunk.get('index') != index:
                raise PackError('worker evidence chunk key and index disagree')
            reply = collector.accept(chunk)
            references[index] = part_reference
        self.header, self.header_reference, self.collector = header, reference, collector
        self.references, self.expected_binding = references, expected
        return reply

    def recover(self, header_reference=None):
        key = self.prefix+'header.json'
        if header_reference is None:
            header, reference = self._discover(key)
        else:
            if (not isinstance(header_reference, dict) or set(header_reference) != {'key','version','sha256'}
                    or header_reference['key'] != key or not isinstance(header_reference['sha256'], str)
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', header_reference['sha256'])):
                raise PackError('invalid worker evidence reference')
            header, reference = self._read(key, header_reference['version'], header_reference['sha256'])
        collector = self._validate_header(header)
        references, reply = [], None
        for index in range(header['count']):
            chunk, part_reference = self._discover(self.prefix+f'chunk-{index:03d}.json')
            if chunk.get('index') != index:
                raise PackError('worker evidence chunk key and index disagree')
            reply = collector.accept(chunk)
            references.append(part_reference)
        if reply is None:
            raise PackError('worker evidence remains incomplete')
        return {'reply':reply, 'header':reference, 'chunks':references, 'qualification':False}
