"""Run reviewed deterministic semantic probes against an exact candidate endpoint.

Recipes contain existing HTTP operations and JSON-pointer projections, not a new
workflow language. They are controller-only, frozen before execution and tied to
observed interface schemas. No model output is an input to this observer.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request

from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, portable_path
from agent_e2e_semantics import CASES, verify, pointer, response_evidence, project_observation, MAX_RESPONSE_BYTES
import agent_e2e_deployment as deployments


def require(condition, reason):
    if not condition:
        raise PackError(reason)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PackError('semantic observer refuses endpoint redirects')


class HttpObserver:
    def __init__(self, endpoint, credential_env):
        parsed = urllib.parse.urlsplit(endpoint)
        require(parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')),
                'semantic endpoint must use TLS or loopback')
        require(not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
                'invalid candidate endpoint')
        self.endpoint = endpoint.rstrip('/')
        self.token = os.environ[credential_env]
        require(bool(self.token), 'missing observer credential')
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, spec):
        require(set(spec) <= {'method', 'path', 'body'} and {'method', 'path'} <= set(spec), 'invalid observer request')
        path = spec['path']
        require(isinstance(path, str) and path.startswith('/') and not path.startswith('//') and '\\' not in path,
                'observer request must stay on candidate origin')
        require(spec['method'] in ('GET', 'POST'), 'unreviewed observer method')
        body = None if 'body' not in spec else json.dumps(spec['body'], allow_nan=False).encode()
        request = urllib.request.Request(self.endpoint + path, data=body, method=spec['method'],
            headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json', 'Accept':'application/json'})
        try:
            response = self.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(MAX_RESPONSE_BYTES+1)
            require(len(raw) <= MAX_RESPONSE_BYTES, 'semantic response exceeds bound')
            # Refuse to persist known observer credentials, including server echo.
            require(self.token.encode() not in raw, 'response disclosed observer credential')
            return response_evidence(response.code, raw)


def save(root, path, value):
    portable_path(path)
    target = root/path
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, sort_keys=True, allow_nan=False)+'\n').encode()
    with target.open('xb') as stream:
        stream.write(data)
    return {'path': path, 'sha256': digest_bytes(data)}


def run(config, campaign, destination, *, observer_factory=HttpObserver):
    require(config['schemaVersion'] == 1, 'unsupported semantic probe configuration')
    require(config['componentBindingDigest'] == campaign['componentBindingDigest'] and config['knowledge'] == campaign['knowledge'],
            'semantic configuration differs from campaign')
    require(set(config['cases']) == set(CASES), 'semantic recipes must cover the complete suite')
    require(isinstance(config['runtimeInterfaces'], dict) and bool(config['runtimeInterfaces']), 'missing observed interface schemas')
    deployments.identity(config['deployment'], campaign, config['target'])
    require(config['deployment']['endpoint'] == config['endpoint'], 'observer endpoint differs from observed deployment')
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    observer = observer_factory(config['endpoint'], config['credentialEnv'])
    deployment = save(root, 'deployment.json', config['deployment'])
    def bound_save(path, value):
        return save(root, path, deployments.bind(value, deployment['sha256'], config['endpoint']))
    interfaces = save(root, 'runtime-interfaces.json', config['runtimeInterfaces'])
    checks = []
    for case in CASES:
        recipe = config['cases'][case]
        require(set(recipe) == {'requests', 'result'}, 'invalid semantic recipe')
        require(isinstance(recipe['requests'], list) and bool(recipe['requests']), 'semantic case has no API operations')
        raw_refs = []
        before = observer.request(config['sourceRequest'])
        require(before['httpStatus'] == 200, 'cannot observe immutable source before probe')
        raw_refs.append(bound_save(f'{case}/source-before.json', before))
        responses = []
        for index, request in enumerate(recipe['requests']):
            response = observer.request(request)
            responses.append(response)
            raw_refs.append(bound_save(f'{case}/response-{index}.json', response))
        after = observer.request(config['sourceRequest'])
        require(after['httpStatus'] == 200, 'cannot observe immutable source after probe')
        raw_refs.append(bound_save(f'{case}/source-after.json', after))
        # Every result field comes from an actual response. Literal expected
        # values and executable projection expressions are deliberately absent.
        projection = {'sourcePointer': config['sourcePointer'], 'result': recipe['result']}
        projection_ref = save(root, f'{case}/projection.json', projection)
        observed = project_observation([before, *responses, after], projection)
        observation = bound_save(f'{case}/observation.json', observed)
        verify(case, observed)
        checks.append({'id': case, 'observation': observation, 'rawResponses': raw_refs, 'projection': projection_ref})
    return {'target': config['target'], 'deployment': deployment, 'runtimeInterfaces': interfaces, 'checks': checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--config-digest', required=True)
    parser.add_argument('--campaign', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(digest_bytes(args.config.read_bytes()) == args.config_digest, 'semantic recipes changed after review')
    result = run(load_json(args.config), load_json(args.campaign), args.output)
    (args.output/'configuration-report.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
