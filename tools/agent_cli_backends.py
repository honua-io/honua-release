"""Controller-side MCP backends and the stage-one remote quickstart shell.

These objects are held by the runner, never serialized into a coding CLI's
configuration. Provider and slot credentials stay in the appropriate process.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import shlex
import socket
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit
import urllib.request

from agent_cli_sessions import digest, screen_model_input
from agent_e2e_local_recorder import write_private

MAX_RESPONSE = 4 * 1024 * 1024
_SAFE_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z')
_UNSAFE_IAM = re.compile(r'AdministratorAccess|iam:\*|CreateAccessKey|CreateUser\b|'
                         r'aws_access_key_id|aws_secret_access_key|\bAKIA[A-Z0-9]{16}\b', re.I)


def _screen(value, secrets):
    return screen_model_input(value, secrets)


class StdioMcpBackend:
    """Runner-owned honua-devops --mcp process with explicit slot credentials."""
    def __init__(self, binary, *, slot_env, workdir, timeout=2700):
        self.binary = str(Path(binary).resolve())
        if not Path(self.binary).is_file() or not os.access(self.binary, os.X_OK):
            raise ValueError('published honua-devops MCP binary is unavailable')
        root = Path(workdir).resolve()
        if not root.is_dir() or any((p / name).exists() for p in (root, *root.parents)
                                    for name in ('.git', 'AGENTS.md', 'CLAUDE.md')):
            raise ValueError('operator must run in a scratch directory outside repositories')
        if not isinstance(slot_env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                or not (k.startswith(('HONUA_DEVOPS_', 'HONUA_IAC_')) or k in (
                    'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                    'AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_ROLE_ARN', 'AWS_ROLE_SESSION_NAME',
                    'AWS_WEB_IDENTITY_TOKEN_FILE')) for k, v in slot_env.items()):
            raise ValueError('operator environment must contain only explicit slot configuration')
        aws = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')
        if any(slot_env.get(k) for k in aws) and not all(slot_env.get(k) for k in aws):
            raise ValueError('operator AWS credentials must be a short-lived session triple')
        role, token_file = slot_env.get('AWS_ROLE_ARN'), slot_env.get('AWS_WEB_IDENTITY_TOKEN_FILE')
        if bool(role) != bool(token_file) or token_file and any(slot_env.get(k) for k in aws):
            raise ValueError('OIDC role and token file must be paired and cannot mix with session keys')
        oidc_token = None
        if token_file:
            token_path = Path(token_file)
            resolved = token_path.resolve()
            if (not re.fullmatch(r'arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+', role)
                    or token_path.is_symlink() or root not in resolved.parents or not resolved.is_file()
                    or resolved.stat().st_mode & 0o077):
                raise ValueError('OIDC token must be private to the slot and paired with a role ARN')
            oidc_token = resolved.read_text().strip()
            if not 20 <= len(oidc_token) <= 16384 or any(c.isspace() for c in oidc_token):
                raise ValueError('invalid OIDC session token')
        self.env = {'HOME': str(root), 'PATH': '/usr/local/bin:/usr/bin:/bin',
                    'LANG': 'C.UTF-8', **slot_env}
        self.secrets = [v for k, v in slot_env.items() if any(s in k for s in ('KEY', 'SECRET', 'TOKEN', 'PASSWORD'))
                        and v and not k.endswith(('_KEY_ARNS', '_KEY_ARN'))]
        if oidc_token: self.secrets.append(oidc_token)
        self.credential_mode = 'web-identity' if token_file else 'session' if all(slot_env.get(k) for k in aws) else None
        self.timeout = timeout
        self.process = None
        self.audit_path = root / 'honua-devops-stderr.log'
        self.audit_sha256 = None
        self.queue = queue.Queue(maxsize=128)
        self.lock = threading.Lock()
        self.counter = 0

    def start(self):
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen([self.binary, '--mcp'], cwd=self.env['HOME'], env=self.env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, start_new_session=True)
        def reader():
            while line := self.process.stdout.readline(MAX_RESPONSE + 1):
                self.queue.put(line)
            self.queue.put(None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            hello = self.rpc('initialize', {'protocolVersion':'2025-03-26','capabilities':{},
                                            'clientInfo':{'name':'honua-arc-runner','version':'1'}})
            if (hello.get('protocolVersion') != '2025-03-26' or not isinstance(hello.get('capabilities'),dict)
                    or not isinstance(hello.get('serverInfo'),dict)):
                raise ValueError('operator MCP initialization was not verified')
            self._send({'jsonrpc':'2.0','method':'notifications/initialized'})
        except Exception:
            self.close()
            raise
        return self

    def _send(self, message):
        self.process.stdin.write(json.dumps(message, separators=(',', ':')).encode() + b'\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        with self.lock:
            import time
            deadline = time.monotonic() + self.timeout
            self.counter += 1
            self._send({'jsonrpc':'2.0','id':self.counter,'method':method,'params':params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError('operator MCP response timed out')
                try: raw = self.queue.get(timeout=remaining)
                except queue.Empty: raise TimeoutError('operator MCP response timed out') from None
                if raw is None or len(raw) > MAX_RESPONSE: raise ValueError('operator MCP stream failed')
                answer = json.loads(raw)
                if answer.get('id') != self.counter: continue
                if 'error' in answer: raise ValueError('operator MCP request failed')
                return _screen(answer['result'], self.secrets)

    def tool_view(self):
        tools, seen, cursor = [], set(), None
        for _ in range(32):
            page = self.rpc('tools/list', {'cursor':cursor} if cursor else {})
            tools.extend(page['tools'])
            cursor = page.get('nextCursor')
            if not cursor: return {'tools':tools}
            if cursor in seen: raise ValueError('repeated MCP tool cursor')
            seen.add(cursor)
        raise ValueError('operator MCP catalog exceeds page bound')

    def execute(self, kind, action):
        if kind != 'tool_call': raise ValueError('operator admits tool calls only')
        if _UNSAFE_IAM.search(json.dumps(action.get('arguments',{}),ensure_ascii=False)):
            return {'status':'fail','refusal':'unsafe-iam-request',
                    'result':{'content':[{'type':'text','text':'Request refused by the slot policy.'}],
                              'isError':True}}
        result = self.rpc('tools/call', {'name':action['tool'],'arguments':action['arguments']})
        return {'status':'fail' if result.get('isError') else 'ok', 'result':result}

    def close(self):
        if self.process is None: return
        self.process.terminate()
        try: self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout): stream.close()
        self.stderr.seek(0)
        raw = self.stderr.read(MAX_RESPONSE + 1)
        self.stderr.close()
        if len(raw) > MAX_RESPONSE or any(secret.encode() in raw for secret in self.secrets):
            raise ValueError('operator audit output exceeded its bound or disclosed a credential')
        for line in raw.decode('utf-8',errors='replace').splitlines():
            _screen(line, self.secrets)
        write_private(self.audit_path, raw)
        self.audit_sha256 = digest(raw)
        self.process = None


class HttpMcpBackend:
    """Forward to a runner-provided loopback browser or validated server MCP endpoint."""
    def __init__(self, endpoint, *, header=None, token=None, timeout=2700, workflow_view=None):
        parts = urlsplit(endpoint)
        valid_remote = (parts.scheme == 'https' and parts.port in (None,443)
                        and re.fullmatch(r'[a-z0-9-]+\.cert\.demo\.honua\.io', parts.hostname or ''))
        valid_loopback = parts.scheme == 'http' and parts.hostname in ('127.0.0.1','localhost','::1')
        if not (valid_remote or valid_loopback) or parts.username or parts.password or parts.fragment or parts.query:
            raise ValueError('MCP endpoint is not a loopback tunnel or certification cell')
        if header not in (None, 'Authorization', 'X-API-Key') or bool(header) != bool(token):
            raise ValueError('invalid controller-side MCP authentication')
        if workflow_view not in (None, 'full'): raise ValueError('unsupported MCP workflow view')
        if workflow_view == 'full' and not token: raise ValueError('full MCP catalog requires authentication')
        self.endpoint, self.header, self.token, self.timeout = endpoint, header, token, timeout
        self.workflow_view = workflow_view
        self.counter = 0
        self.lock = threading.Lock()
        self.session_id = None

    def rpc(self, method, params=None, *, notification=False):
        with self.lock:
            self.counter += 1
            message = {'jsonrpc':'2.0','method':method,'params':params or {}}
            if not notification: message['id'] = self.counter
            headers = {'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
            if self.header: headers[self.header] = self.token
            if self.session_id: headers['Mcp-Session-Id'] = self.session_id
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *_args, **_kwargs): return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            request = urllib.request.Request(self.endpoint, data=json.dumps(message).encode(), headers=headers)
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                self.session_id = response.headers.get('Mcp-Session-Id', self.session_id)
            if notification: return {}
            if len(raw) > MAX_RESPONSE: raise ValueError('MCP response exceeds bound')
            from agent_e2e_local_recorder import parse_jsonrpc
            answer = parse_jsonrpc(raw)
            if answer.get('id') != message['id'] or 'error' in answer: raise ValueError('MCP request failed')
            return _screen(answer['result'], [self.token, self.token.removeprefix('Bearer ')] if self.token else [])

    def start(self):
        params = {'protocolVersion':'2025-03-26','capabilities':{},
                  'clientInfo':{'name':'honua-arc-runner','version':'1'}}
        if self.workflow_view: params['_meta'] = {'honua.io/workflow-view':self.workflow_view}
        hello = self.rpc('initialize', params)
        if (hello.get('protocolVersion') != '2025-03-26' or not isinstance(hello.get('capabilities'),dict)
                or not isinstance(hello.get('serverInfo'),dict)):
            raise ValueError('MCP initialization was not verified')
        self.rpc('notifications/initialized', notification=True)
        return self

    def tool_view(self):
        tools, seen, cursor = [], set(), None
        for _ in range(32):
            params = {'view':self.workflow_view} if self.workflow_view else {}
            if cursor: params['cursor'] = cursor
            page = self.rpc('tools/list', params)
            # Honua's full export is the canonical paginated MCP result and has
            # no view metadata; narrowed workflow projections carry that metadata.
            if self.workflow_view and '_meta' in page and (not isinstance(page['_meta'],dict)
                    or page['_meta'].get('view') != self.workflow_view):
                raise ValueError('server returned a narrowed tool view')
            tools.extend(page['tools'])
            cursor = page.get('nextCursor')
            if not cursor: return {'tools':tools}
            if cursor in seen: raise ValueError('repeated MCP tool cursor')
            seen.add(cursor)
        raise ValueError('MCP catalog exceeds page bound')

    def execute(self, kind, action):
        if kind == 'resource_read': result = self.rpc('resources/read', {'uri':action['uri']})
        elif kind == 'tool_call': result = self.rpc('tools/call', {'name':action['tool'],'arguments':action['arguments']})
        else: raise ValueError('MCP backend admits only MCP actions')
        return {'status':'fail' if result.get('isError') else 'ok', 'result':result}

    def close(self): pass


SHELL_TOOL = {'name':'run_quickstart', 'description':'Run an allowed Docker Compose or localhost curl command on the isolated quickstart host.',
              'inputSchema':{'type':'object','properties':{'command':{'type':'string','maxLength':2048},
                  'stdin':{'type':'string','maxLength':65536}},
                             'required':['command'],'additionalProperties':False}}


def parse_quickstart(command):
    if not isinstance(command, str) or not command or len(command) > 2048 or any(c in command for c in '\n\r\x00'):
        raise ValueError('invalid quickstart command')
    parts = shlex.split(command)
    if any(not token or any(c in token for c in ';|&`$<>\\') for token in parts):
        raise ValueError('shell syntax is not allowed')
    if parts[:2] == ['docker','compose']:
        options, position = [], 2
        while position < len(parts) and parts[position] in ('-f','--file','--profile'):
            if (position + 1 >= len(parts) or not (_SAFE_ID.fullmatch(parts[position+1])
                    or parts[position] in ('-f','--file') and parts[position+1] == '-')):
                raise ValueError('invalid compose option')
            options += parts[position:position+2]; position += 2
        if position >= len(parts) or parts[position] not in ('up','pull','ps','logs','config','version'):
            raise ValueError('compose operation is not admitted')
        operation = parts[position]; rest = parts[position+1:]
        allowed = {'up':{'-d','--detach','--wait','--no-build','--quiet-pull','--wait-timeout'},
                   'pull':{'--quiet'},'ps':{'-a','--all','--format=json'},
                   'logs':{'--no-color','--timestamps'},'config':{'--quiet'},'version':set()}[operation]
        if '--wait-timeout' in rest:
            if rest.count('--wait-timeout') != 1: raise ValueError('duplicate compose wait timeout')
            ix=rest.index('--wait-timeout')
            if ix+1 >= len(rest) or not rest[ix+1].isdigit() or not 1 <= int(rest[ix+1]) <= 300:
                raise ValueError('invalid compose wait timeout')
        if any(token.startswith('-') and token not in allowed or not token.startswith('-') and not _SAFE_ID.fullmatch(token)
               for token in rest): raise ValueError('compose argument is not admitted')
        if operation in ('config','version') and rest: raise ValueError('compose operation takes no arguments')
        return ['docker','compose',*options,operation,*rest]
    if parts and parts[0] == 'curl':
        opts, urls, position = [], [], 1
        while position < len(parts):
            token = parts[position]
            if token in ('-f','-s','-S','-sS','-fsS','--fail','--silent','--show-error','-i','--include'):
                opts.append(token)
            elif token.startswith('-'): raise ValueError('curl option is not admitted')
            else: urls.append(token)
            position += 1
        if len(urls) != 1: raise ValueError('exactly one localhost URL is required')
        url = urlsplit(urls[0])
        if (url.scheme not in ('http','https') or url.hostname not in ('localhost','127.0.0.1','::1')
                or url.username or url.password or url.fragment): raise ValueError('curl is restricted to localhost')
        return ['curl','--max-time','30','--proto','=http,https',*opts,urls[0]]
    raise ValueError('only docker compose and localhost curl are admitted')


class RemoteQuickstartShell:
    """Run parsed commands on a runner-owned remote VM in an isolated slot directory."""
    def __init__(self, *, host, user, identity_file, known_hosts, remote_dir, slot, executor=subprocess.run,
                 approved_compose_sha256=None, visible_document_sha256=()):
        if not _SAFE_ID.fullmatch(host) or host.lower() in ('localhost','127.0.0.1'):
            raise ValueError('a remote VM host is required')
        try:
            if ipaddress.ip_address(host).is_loopback: raise ValueError('a remote VM host is required')
        except ValueError as exc:
            if str(exc) == 'a remote VM host is required': raise
        if not _SAFE_ID.fullmatch(user) or not re.fullmatch(r'/tmp/honua-arc-[a-z0-9-]{8,80}', remote_dir):
            raise ValueError('invalid isolated VM slot')
        if not re.fullmatch(r'arc-[a-z0-9-]{8,50}', slot): raise ValueError('invalid compose project')
        if not Path(identity_file).is_file() or not Path(known_hosts).is_file(): raise ValueError('SSH identity and host key required')
        self.host, self.user, self.identity = host, user, str(Path(identity_file).resolve())
        self.known_hosts, self.remote_dir, self.slot = str(Path(known_hosts).resolve()), remote_dir, slot
        self.executor = executor
        hashes = tuple(visible_document_sha256)
        if any(not re.fullmatch(r'sha256:[0-9a-f]{64}', h) for h in hashes) or (
                approved_compose_sha256 is not None and not re.fullmatch(r'sha256:[0-9a-f]{64}', approved_compose_sha256)):
            raise ValueError('invalid approved quickstart digest')
        self.approved_compose_sha256, self.visible_document_sha256 = approved_compose_sha256, hashes

    def tool_view(self): return {'tools':[SHELL_TOOL]}

    def _ssh(self, script):
        ssh = ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
               '-o','UserKnownHostsFile='+self.known_hosts,'-i',self.identity,
               '--',self.user+'@'+self.host,'sh','-s']
        return self.executor(ssh, input=script.encode(), capture_output=True, timeout=2700,
                             env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(Path(self.identity).parent)})

    def check_health(self):
        result = self._ssh("docker info --format '{{.ServerVersion}}' && docker ps --format '{{.Status}}'")
        if result.returncode or b'unhealthy' in result.stdout.lower():
            raise RuntimeError('remote VM Docker is unhealthy; do not start the case')
        return {'status':'pass','observationSha256':digest(result.stdout)}

    def execute(self, kind, action):
        if kind != 'shell_command' or action.get('tool') != 'run_quickstart': raise ValueError('invalid shell action')
        args = action.get('arguments', {})
        if not isinstance(args,dict) or 'command' not in args or set(args) - {'command','stdin'}:
            raise ValueError('invalid shell arguments')
        command = parse_quickstart(args['command'])
        inline = any(command[i] in ('-f','--file') and command[i+1] == '-' for i in range(len(command)-1))
        source = args.get('stdin')
        if inline:
            if (not isinstance(source,str) or not source.endswith('\n') or len(source.encode()) > 65536 or '\x00' in source
                    or digest(source.encode()) != self.approved_compose_sha256):
                raise ValueError('inline compose must match the runner-approved published quickstart')
        elif source is not None:
            raise ValueError('stdin is only allowed for an approved inline compose file')
        operation = None
        if command[:2] == ['docker','compose']:
            index = 2
            while index < len(command) and command[index] in ('-f','--file','--profile'): index += 2
            operation = command[index]
            if operation == 'ps' and '--format=json' not in command: command.append('--format=json')
            command[2:2] = ['--project-directory',self.remote_dir,'--project-name',self.slot]
        script = 'cd -- ' + shlex.quote(self.remote_dir) + ' && exec ' + shlex.join(command)
        if inline:
            fence = 'HONUA_' + self.approved_compose_sha256[7:39]
            if fence in source: raise ValueError('invalid compose input delimiter')
            script += " <<'" + fence + "'\n" + source + fence + '\n'
        result = self._ssh(script)
        # A local quickstart can generate secrets in .env; compose config/logs and
        # arbitrary HTTP responses must not turn those into model-visible bytes.
        raw = result.stdout[:MAX_RESPONSE]
        if command[0] == 'curl' and digest(raw) in self.visible_document_sha256 and len(raw) <= 65536:
            visible = raw.decode('utf-8')
        elif command[0] == 'curl' and raw.strip() in (b'Ready',b'Healthy',b'OK',b'Unhealthy'):
            visible = raw.decode('utf-8')
        elif operation == 'ps':
            try:
                try: rows = json.loads(raw)
                except ValueError: rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                if isinstance(rows,dict): rows = [rows]
                if not isinstance(rows,list) or len(rows) > 100: raise ValueError('invalid compose status')
                visible = json.dumps([{key:value for key in ('Name','Service','State','Health')
                                       if isinstance((value:=row.get(key)),str) and _SAFE_ID.fullmatch(value)}
                                      for row in rows if isinstance(row,dict)])
            except ValueError:
                visible = f'Command exited {result.returncode}; status output unavailable; sha256 {digest(result.stdout)}'
        else:
            visible = f'Command exited {result.returncode}; output withheld; sha256 {digest(result.stdout)}'
        _screen(visible, [])
        return {'status':'ok' if result.returncode == 0 else 'fail',
                'result':{'content':[{'type':'text','text':visible}],
                          'isError':result.returncode != 0, 'exitCode':result.returncode},
                '_recording':{'commandSha256':digest(script.encode()), 'stdoutSha256':digest(result.stdout),
                              'stderrSha256':digest(result.stderr)}}


class SshTunnel:
    """Runner-owned localhost tunnel to the isolated VM; never given to the CLI as SSH access."""
    def __init__(self, remote, *, remote_port, popen=subprocess.Popen):
        if not isinstance(remote, RemoteQuickstartShell) or type(remote_port) is not int or not 1 <= remote_port <= 65535:
            raise ValueError('invalid remote VM tunnel')
        self.remote, self.remote_port, self.popen = remote, remote_port, popen
        self.process = None

    def start(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
            port = listener.getsockname()[1]
        remote = self.remote
        command = ['ssh','-N','-T','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes',
                   '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+remote.known_hosts,
                   '-i',remote.identity,'-L',f'127.0.0.1:{port}:127.0.0.1:{self.remote_port}',
                   '--',remote.user+'@'+remote.host]
        self.process = self.popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True,
                                  env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(Path(remote.identity).parent)})
        import time
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if self.process.poll() is not None: raise RuntimeError('SSH tunnel exited before readiness')
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2): break
            except OSError: time.sleep(.05)
        else:
            self.close()
            raise TimeoutError('SSH tunnel did not become ready')
        self.base_url = f'http://127.0.0.1:{port}'
        return self

    def close(self):
        if self.process is None: return
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
