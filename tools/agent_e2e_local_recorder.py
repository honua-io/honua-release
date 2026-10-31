"""Runner-owned controller recorder and private model action log.

Scenarios read live state only through this recorder. It authenticates with
runner-held credentials (resolved from the environment at call time, never handed
to a scenario), retains every response's exact bytes and sha256 under a unique
step name, and is the only source the scorer accepts evidence from.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProjectionError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ProjectionError(reason)


STEP = re.compile(r'[a-z0-9-]{1,80}')
PHASES = ('seed', 'session', 'observe')
# 'between' is a temporary phase the runner opens between two model sessions (restart, approval).


@dataclass(frozen=True)
class Record:
    """One retained response. Only the raw bytes are stored; every parsed view is re-derived
    from them on access, so no caller can change what a projection reads without changing the
    bytes, whose digest the runner checks against its own ledger at scoring time."""
    step: str
    phase: str
    kind: str                      # 'http' | 'mcp'
    principal: str                 # recorder principal name
    principal_id: str | None       # credential/principal id when the runner knows it
    method: str
    path: str
    status: int
    raw: bytes
    sha256: str                    # 'sha256:<hex>' of raw
    replay_of: str | None = None   # action-log digest for a replayed model request
    request_arguments_raw: bytes | None = None
    origin: str = 'controller'     # 'controller' read, or 'session-result': the server's own response to
                                   # a sealed model action, captured by the runner gateway or proxy
    content_type: str | None = None
    recorded_at: str = ''

    def metadata(self, case_id) -> dict:
        """Everything about the record the ledger binds: request identity, principal, phase, status, body."""
        return {'case': case_id, 'step': self.step, 'phase': self.phase, 'kind': self.kind,
                'principal': self.principal, 'principalId': self.principal_id, 'method': self.method,
                'path': self.path, 'status': self.status, 'bodySha256': 'sha256:' + hashlib.sha256(self.raw).hexdigest(),
                'replayOf': self.replay_of, 'origin': self.origin, 'contentType': self.content_type,
                'recordedAt': self.recorded_at,
                'requestArgumentsSha256': (None if self.request_arguments_raw is None else
                                           'sha256:' + hashlib.sha256(self.request_arguments_raw).hexdigest())}

    @property
    def json(self) -> Any:
        """Parsed body (HTTP) or the JSON-RPC response message (MCP), fresh from raw bytes."""
        if self.kind == 'mcp':
            return parse_jsonrpc(self.raw)
        try:
            return json.loads(self.raw) if self.raw.strip() else None
        except ValueError:
            return None

    @property
    def request_arguments(self) -> dict | None:
        """MCP records only: the {"tool", "arguments"} (or {"uri"}) that was sent. HTTP records return None;
        their request body is kept as raw bytes in ``request_arguments_raw`` and compared byte for byte."""
        if self.request_arguments_raw is None:
            return None
        try:
            value = json.loads(self.request_arguments_raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) and self.kind == 'mcp' else None

    def intact(self) -> bool:
        return 'sha256:' + hashlib.sha256(self.raw).hexdigest() == self.sha256

    def index(self) -> dict:
        row = {'step': self.step, 'phase': self.phase, 'kind': self.kind, 'principal': self.principal,
               'method': self.method, 'path': self.path, 'status': self.status, 'sha256': self.sha256}
        if self.principal_id:
            row['principalId'] = self.principal_id
        if self.replay_of:
            row['replayOf'] = self.replay_of
        if self.origin != 'controller':
            row['origin'] = self.origin
        return row


@dataclass(frozen=True)
class LoggedAction:
    session: int
    sequence: int
    digest: str
    kind: str
    tool: str | None
    arguments: dict | None
    uri: str | None
    status: str
    path: str


def action_bytes(action: dict) -> bytes:
    """Canonical bytes for an actual model-selected action retained in the private log."""
    return json.dumps(action, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


def read_logged_action(path, digest: str) -> dict:
    raw = Path(path).read_bytes()
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('action log digest mismatch')
    action = json.loads(raw)
    if not isinstance(action, dict) or action_bytes(action) != raw:
        raise ValueError('action log entry is malformed or noncanonical')
    return action


def write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def parse_jsonrpc(raw: bytes) -> dict:
    """The single JSON-RPC response message in a JSON or finite SSE MCP body."""
    from agent_e2e_mcp import parse
    if raw.lstrip().startswith(b'{'):
        messages = [parse(raw)]
    else:
        frames = raw.replace(b'\r\n', b'\n').split(b'\n\n')
        require(not frames[-1].strip(), 'incomplete MCP response stream')
        messages = [parse(data) for frame in frames[:-1]
                    if (data := b'\n'.join(line[5:].lstrip(b' ') for line in frame.split(b'\n')
                                           if line.startswith(b'data:')))]
    responses = [m for m in messages if isinstance(m, dict) and ('result' in m or 'error' in m) and 'id' in m]
    require(len(responses) == 1, 'MCP response is missing or ambiguous')
    return responses[0]


_GENESIS = 'sha256:' + hashlib.sha256(b'honua-corpus-action-log/v1').hexdigest()


def _link(chain, entry):
    return 'sha256:' + hashlib.sha256((chain + '\n' + json.dumps(list(entry), separators=(',', ':')))
                                      .encode()).hexdigest()


class EvidenceVault:
    """Runtime-owned evidence authority for one case: the only record store and action inventory
    that scoring trusts. Scenarios never receive it. The recorder delivers each record here at the
    moment it is retained (one write per step, metadata and body bound by one digest); the runtime
    alone seals model actions, from its own ActionLog, into an append-only hash chain.
    """

    def __init__(self, case_id):
        self.case_id = case_id
        self._records = {}
        self._digests = {}
        self._actions = []          # (entry, action_bytes, chain)
        self._chain = _GENESIS
        self._sessions = set()

    def digest(self, record):
        return 'sha256:' + hashlib.sha256(json.dumps(record.metadata(self.case_id), sort_keys=True,
                                                     separators=(',', ':')).encode()).hexdigest()

    def deliver(self, record):
        require(isinstance(record, Record) and record.step not in self._records, 'vault step already delivered')
        require(record.intact(), 'recorded digest differs from the retained bytes')
        self._records[record.step] = record
        self._digests[record.step] = self.digest(record)

    def records(self):
        """Records whose metadata and bytes still match what was delivered."""
        out = {}
        for step, record in self._records.items():
            require(self.digest(record) == self._digests[step] and record.intact(), 'vault record altered: ' + step)
            out[step] = record
        return out

    def ledger(self):
        return dict(self._digests)

    def append_actions(self, session, rows):
        """Runtime only: seal one session's model actions, in order, from the runner's own ActionLog."""
        require(type(session) is int and session > 0 and session not in self._sessions, 'session already sealed')
        self._sessions.add(session)
        for expected, row in enumerate(rows, start=1):
            require(row['session'] == session and row['sequence'] == expected, 'action log is out of order')
            raw = Path(row['logPath']).read_bytes()
            require('sha256:' + hashlib.sha256(raw).hexdigest() == row['digest'], 'action log digest mismatch')
            entry = (session, expected, row['digest'], str(row['logPath']), row['status'],
                     row.get('resultRawSha256'), row.get('resultRawPath'), row.get('resultHttpStatus'))
            self._chain = _link(self._chain, entry)
            self._actions.append((entry, raw, self._chain))

    def entries(self):
        chain = _GENESIS
        for entry, raw, link in self._actions:
            chain = _link(chain, entry)
            require(chain == link and 'sha256:' + hashlib.sha256(raw).hexdigest() == entry[2],
                    'harness action log chain is broken')
        require(chain == self._chain, 'harness action log head differs')
        return [(entry, raw) for entry, raw, _ in self._actions]

    def model_actions(self):
        """digest -> the model's logged action (for replay-provenance checks)."""
        return {entry[2]: json.loads(raw) for entry, raw in self.entries()}


class ControllerRecorder:
    """Retains every controller read once; step names are unique across the case.

    ``principals`` maps a principal name to (credential env var or None for anonymous,
    principal id or None). Values are read from the environment by the transport at call
    time and checked against every response by the transports' own disclosure screens.
    """

    def __init__(self, *, base_url, workdir, candidate_digest, attempt_id, principals,
                 http_factory=None, mcp_factory=None, vault=None, admitted_remote_origin=None):
        self.base_url = base_url.rstrip('/')
        self._admitted_remote_origin = admitted_remote_origin
        if admitted_remote_origin is not None:
            from urllib.parse import urlsplit
            remote = urlsplit(admitted_remote_origin)
            require(remote.scheme == 'https' and not remote.username and not remote.password
                    and remote.port in (None, 443) and remote.path == '' and not remote.query and not remote.fragment
                    and re.fullmatch(r'[a-z0-9-]+\.cert\.demo\.honua\.io', remote.hostname or '')
                    and self.base_url == admitted_remote_origin,
                    'remote recorder requires the exact admitted HTTPS ECS cell origin')
        self.root = Path(workdir) / 'controller-records'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.identity = {'candidateKey': candidate_digest, 'attemptId': attempt_id}
        self.principals = dict(principals)
        self._records: dict[str, Record] = {}
        self.phase = 'seed'
        self._pending = {}
        self._http_factory = http_factory
        self._mcp_factory = mcp_factory
        vault = vault if vault is not None else EvidenceVault(attempt_id)
        # Only these two capabilities of the runtime-owned vault are kept: deliver a new record, and
        # read the sealed action inventory. The recorder can neither seal actions nor rewrite records.
        self._deliver = vault.deliver
        self._sealed_entries = vault.entries
        self._case = vault.case_id

    @property
    def records(self):
        """Read-only view; records are frozen and their parsed views are re-derived from bytes."""
        from types import MappingProxyType
        return MappingProxyType(self._records)


    # -- lifecycle ---------------------------------------------------------
    def between(self):
        """Runner only: open controller reads between two model sessions (records carry phase 'between')."""
        import contextlib
        recorder = self

        @contextlib.contextmanager
        def window():
            require(recorder.phase == 'session', 'between-session window outside the session phase')
            recorder.phase = 'between'
            try:
                yield recorder
            finally:
                recorder.phase = 'session'
        return window()

    def set_phase(self, phase):
        require(self.phase in PHASES and phase in PHASES and PHASES.index(phase) >= PHASES.index(self.phase),
                'recorder phase cannot go back')
        self.phase = phase

    def index(self):
        return [record.index() for record in self._records.values()]

    def _claim(self, step):
        require(isinstance(step, str) and STEP.fullmatch(step), 'invalid recorder step name')
        require(step not in self._records and step not in self._pending,
                'recorder step already used: ' + step)
        require(self.phase != 'session', 'controller reads are closed while the model session runs')

    def _principal(self, name):
        require(name in self.principals, 'unknown recorder principal: ' + str(name))
        return self.principals[name]

    def _store(self, record: Record):
        import dataclasses
        require(record.intact(), 'recorded digest differs from the retained bytes')
        record = dataclasses.replace(record, recorded_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        self._deliver(record)
        self._records[record.step] = record
        row = {**record.index(), 'bodyBase64': base64.b64encode(record.raw).decode('ascii')}
        if record.request_arguments_raw is not None:
            row['requestArgumentsSha256'] = 'sha256:' + hashlib.sha256(record.request_arguments_raw).hexdigest()
        write_private(self.root / (record.step + '.json'), json.dumps(row, sort_keys=True).encode())
        return record

    # -- HTTP --------------------------------------------------------------
    def _request(self, step, principal, method, path, body, content_type):
        """One request to the local stack or explicitly admitted cell; retain every response."""
        import urllib.error
        import urllib.parse
        import urllib.request
        self._claim(step)
        env, identity = self._principal(principal)
        require(method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') and isinstance(path, str)
                and path.startswith('/') and not path.startswith('//') and all(33 <= ord(c) <= 126 for c in path),
                'invalid recorder request')
        require(urllib.parse.urlsplit(self.base_url).hostname in ('127.0.0.1', 'localhost', '::1')
                or self._admitted_remote_origin is not None and self.base_url == self._admitted_remote_origin,
                'recorder reads only the case-local stack or explicitly admitted cell')
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = content_type
        token = None
        if env is not None:
            token = os.environ.get(env)
            require(isinstance(token, str) and token, 'recorder credential is not available')
            headers['X-API-Key'] = token
        content_type = None
        if self._http_factory is not None:
            status, raw = self._http_factory(self, principal, method, path, body, headers)
        else:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
            self._pending[step] = (principal, method, path)
            try:
                try:
                    response = opener.open(request, timeout=120)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status, raw = response.status if hasattr(response, 'status') else response.code, response.read(16 * 1024 * 1024 + 1)
                    content_type = response.headers.get('Content-Type')
            finally:
                self._pending.pop(step, None)
            require(len(raw) <= 16 * 1024 * 1024, 'recorded response exceeds bound')
        require(token is None or token.encode() not in raw, 'response disclosed the requesting credential')
        # The request body is part of the request identity (bound in the vault digest): two POST
        # enumerations with different body-defined scopes are different requests.
        record = self._store(Record(step, self.phase, 'http', principal, identity, method, path, status, raw,
                                    'sha256:' + hashlib.sha256(raw).hexdigest(), request_arguments_raw=body,
                                    content_type=content_type))
        return {'observation': {'httpStatus': status, 'body': record.json, 'responseDigest': record.sha256,
                                'rawBodyBase64': base64.b64encode(raw).decode('ascii')},
                'reference': {'key': step, 'sha256': record.sha256}}

    def json(self, step, method, path, body=None, *, principal='controller'):
        encoded = None if body is None else json.dumps(body, ensure_ascii=True, allow_nan=False,
                                                       separators=(',', ':')).encode()
        return self._request(step, principal, method, path, encoded, 'application/json')

    def upload(self, step, raw, *, table, schema, srid, principal='controller'):
        """The fixture import upload (same multipart form as agent_e2e_setup_http.SetupHttp.upload)."""
        require(type(raw) is bytes and all(isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', v)
                                           for v in (table, schema)) and type(srid) is int and srid > 0,
                'invalid fixture upload target')
        boundary = 'honua-' + hashlib.sha256(raw).hexdigest()
        require(boundary.encode() not in raw, 'fixture upload contains multipart boundary')
        fields = {'TableName': table, 'TargetSchema': schema, 'SourceSrid': str(srid),
                  'TargetSrid': str(srid), 'OverwriteExisting': 'false'}
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                 for key, value in fields.items()]
        parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fixture.geojson"\r\n'
                  'Content-Type: application/geo+json\r\n\r\n'.encode(), raw, f'\r\n--{boundary}--\r\n'.encode()]
        return self._request(step, principal, 'POST', '/api/v1/admin/import/upload', b''.join(parts),
                             'multipart/form-data; boundary=' + boundary)

    # -- MCP ---------------------------------------------------------------
    def _mcp(self, step, principal, rpc, *, replay_of=None, arguments=None, label):
        from agent_e2e_mcp import McpError
        self._claim(step)
        env, identity = self._principal(principal)
        require(env is not None, 'MCP reads need an authenticated principal')
        if self._mcp_factory is not None:
            client = self._mcp_factory(self, principal)
        else:
            from agent_e2e_mcp import Client
            client = Client(self.base_url + '/mcp', credential_env=env, credential_kind='api-key', timeout=120)
        self._pending[step] = (principal, 'POST', '/mcp')
        error = None
        try:
            client.initialize()
            before = len(client.observations)
            try:
                result = rpc(client)
            except McpError as exc:
                if exc.category != 'rpc-error':
                    raise
                result, error = None, exc
            rows = [row for row in client.observations[before:] if row.get('rpcMethod') == label]
        finally:
            try:
                client.close()
            finally:
                self._pending.pop(step, None)
        require(len(rows) == 1, 'MCP read is missing or ambiguous')
        raw = base64.b64decode(rows[0]['bodyBase64'], validate=True)
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        require(digest == rows[0]['bodyDigest'], 'recorded MCP response digest mismatch')
        message = parse_jsonrpc(raw)
        if error is None:
            require(message.get('result') == result, 'MCP client result differs from raw response')
        self._store(Record(step, self.phase, 'mcp', principal, identity, 'POST', '/mcp#' + label,
                           rows[0]['httpStatus'], raw, digest, replay_of,
                           None if arguments is None else action_bytes(arguments)))
        if error is not None:
            raise error
        return result

    def mcp_call(self, step, tool, arguments, *, principal='controller'):
        require(isinstance(tool, str) and isinstance(arguments, dict), 'invalid MCP call')
        return self._mcp(step, principal, lambda c: c.call_tool(tool, arguments), arguments={'tool': tool, 'arguments': arguments},
                         label='tools/call')

    def mcp_read(self, step, uri, *, principal='controller'):
        return self._mcp(step, principal, lambda c: c.read_resource(uri), arguments={'uri': uri}, label='resources/read')

    # -- replay of the harness action log ----------------------------------
    def _verified_entries(self):
        return [entry for entry, _raw in self._sealed_entries()]

    def _sealed_action(self, action):
        member = (action.session, action.sequence, action.digest, str(action.path), action.status)
        found = [(entry, raw) for entry, raw in self._sealed_entries() if entry[:5] == member]
        require(len(found) == 1, 'action is not in this case\'s harness action log')
        return found[0]

    def actions(self):
        """Digest-verified model actions of this case, in execution order (the harness's own log)."""
        out = []
        for (session, sequence, digest, path, status, *_), raw in self._sealed_entries():
            action = json.loads(raw)
            out.append(LoggedAction(session, sequence, digest, action.get('kind'), action.get('tool'),
                                    action.get('arguments'), action.get('uri'), status, path))
        return tuple(out)

    def replay(self, step, action: LoggedAction, *, principal='controller'):
        """Re-execute one sealed model tool call of this case verbatim with controller credentials."""
        require(self.phase == 'observe', 'replay happens after the session')
        require(principal == 'controller', 'replay runs with controller credentials')
        _entry, raw = self._sealed_action(action)
        logged = json.loads(raw)  # the vault's own copy of the model's bytes, not the file
        require(logged.get('kind') == 'tool_call' and isinstance(logged.get('arguments'), dict)
                and isinstance(logged.get('tool'), str), 'only a logged tool call can be replayed')
        tool, arguments = logged['tool'], json.loads(json.dumps(logged['arguments']))
        return self._mcp(step, principal, lambda c: c.call_tool(tool, arguments), replay_of=action.digest,
                         arguments={'tool': tool, 'arguments': arguments}, label='tools/call')

    def action_result(self, step, action: LoggedAction):
        """Register the server's own response to one sealed model action as observe-phase evidence.

        The bytes were captured by the runner (gateway or recording proxy) when the action ran and
        sealed into the action chain with their digest; the model never supplies them. Use this for
        product reports that exist only as the response to the model's call (e.g. an import's
        per-row errors), and read persisted state with controller reads as well.
        """
        require(self.phase == 'observe', 'action results are registered after the session')
        self._claim(step)
        entry, action_raw = self._sealed_action(action)
        _session, _sequence, digest, path, _status, raw_sha, raw_path, http_status = entry
        require(isinstance(raw_sha, str) and isinstance(raw_path, str), 'no server response was captured for the action')
        raw = Path(raw_path).read_bytes()
        require('sha256:' + hashlib.sha256(raw).hexdigest() == raw_sha, 'captured action response digest mismatch')
        logged = json.loads(action_raw)
        label = 'tools/call' if logged.get('kind') == 'tool_call' else 'resources/read'
        arguments = ({'tool': logged.get('tool'), 'arguments': logged.get('arguments')} if label == 'tools/call'
                     else {'uri': logged.get('uri')})
        _env, identity = self.principals.get('agent', (None, None))
        return self._store(Record(step, 'observe', 'mcp', 'agent', identity, 'POST', '/mcp#' + label,
                                  int(http_status or 0), raw, raw_sha, digest, action_bytes(arguments),
                                  'session-result'))
