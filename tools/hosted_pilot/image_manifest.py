"""Bind a native ECS platform digest to the reviewed immutable registry manifest."""
from __future__ import annotations
import hashlib
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ACCEPT = ', '.join(['application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json', 'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])


def require(value, reason):
    if not value:
        raise ValueError(reason)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read(url, headers):
    with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=15) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    require(len(data) <= 4 * 1024 * 1024, 'registry manifest exceeds bounded size')
    return data


def manifest_bytes(image, *, aws=None):
    repository, expected = image.split('@')
    registry, path = repository.split('/', 1)
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', expected) and re.fullmatch(r'[a-z0-9._-]+(/[a-z0-9._-]+)*', path), 'invalid immutable registry image identity')
    ecr = re.fullmatch(r'([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com', registry)
    if ecr:
        require(aws is not None, 'private manifest inspection needs native ECR read access')
        result = aws('ecr', 'batch-get-image', '--region', ecr[2], '--registry-id', ecr[1], '--repository-name', path, '--image-ids', 'imageDigest=' + expected)
        require(not result.get('failures') and len(result.get('images', [])) == 1 and result['images'][0]['imageId']['imageDigest'] == expected, 'native ECR immutable manifest unavailable')
        raw = result['images'][0]['imageManifest'].encode()
    else:
        require(registry in ('ghcr.io', 'quay.io'), 'unsupported registry for public platform-manifest verification')
        url = 'https://' + registry + '/v2/' + path + '/manifests/' + expected
        try:
            raw = read(url, {'Accept': ACCEPT})
        except HTTPError as error:
            require(error.code == 401, 'public immutable manifest unavailable')
            # Fixed same-registry token endpoints. Never follow caller-provided
            # bearer realms or forward a token to another registry/redirect.
            endpoint = '/token' if registry == 'ghcr.io' else '/v2/auth'
            token = json.loads(read('https://' + registry + endpoint + '?' + urlencode({'service': registry, 'scope': 'repository:' + path + ':pull'}), {'Accept': 'application/json'}))
            bearer = token.get('token', token.get('access_token'))
            require(isinstance(bearer, str) and bearer, 'anonymous public pull token unavailable')
            raw = read(url, {'Accept': ACCEPT, 'Authorization': 'Bearer ' + bearer})
    require('sha256:' + hashlib.sha256(raw).hexdigest() == expected, 'registry manifest bytes differ from reviewed SHA256')
    return raw


def verify(image, observed_digest, *, architecture='X86_64', operating_system='LINUX', aws=None):
    expected = image.rsplit('@', 1)[1]
    require(architecture in ('X86_64', 'ARM64') and operating_system == 'LINUX', 'unsupported native task platform')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', observed_digest or ''), 'native ECS image digest missing')
    if observed_digest == expected:
        return {'image': image, 'observedDigest': observed_digest, 'manifestDigest': expected, 'platform': {'os': 'linux', 'architecture': 'amd64' if architecture == 'X86_64' else 'arm64'}, 'binding': 'native-exact-digest'}
    raw = manifest_bytes(image, aws=aws)
    manifest = json.loads(raw)
    require(manifest.get('mediaType') in ('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'), 'native image differs from its reviewed single-image manifest')
    arch = 'amd64' if architecture == 'X86_64' else 'arm64'
    children = [row['digest'] for row in manifest.get('manifests', []) if row.get('platform', {}).get('os') == 'linux' and row.get('platform', {}).get('architecture') == arch]
    require(len(children) == 1 and observed_digest == children[0], 'native image is not the reviewed manifest member for its task platform')
    return {'image': image, 'observedDigest': observed_digest, 'manifestDigest': expected, 'platform': {'os': 'linux', 'architecture': arch}, 'binding': 'verified-platform-manifest-member'}
