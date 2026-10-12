#!/usr/bin/env python3
"""Hosted bounded SDK installer proof; immutable inputs, no source-build fallback.

Stages deliberately retain failures and run cleanup independently. Public outputs
are allowlisted by the workflow; raw command logs and credentials stay private.
This does not certify publication, human approval, AWS or model execution.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import shutil
import yaml

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)


def checked_bytes(path, expected):
    if not re.fullmatch('[0-9a-f]{64}', expected) or digest(path) != expected:
        raise RuntimeError('reviewed file digest mismatch')
    return path.read_text()


def private(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('credential file is not owner-only')


def load_pins(release):
    spec = importlib.util.spec_from_file_location('honua_reviewed_pins', release/'certification/terminal-journey/pins.py')
    pins = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pins
    spec.loader.exec_module(pins)
    return pins


def isolated_env(root, inherited=None):
    """One normal resolver attempt; inherited relaxations cannot turn a failure green."""
    pins = load_pins(HERE.parents[1])
    env = {k: v for k, v in pins.candidate_env(inherited).items() if not k.startswith('HONUA_')}
    for name in ['user.npmrc', 'global.npmrc']:
        (root/name).write_text('')
    env.update(npm_config_userconfig=str(root/'user.npmrc'),
               npm_config_globalconfig=str(root/'global.npmrc'),
               npm_config_cache=str(root/'npm-cache'),
               npm_config_registry='https://registry.npmjs.org')
    return env


def compose_args(root):
    """Only an installer-created random project inside this private run is owned."""
    stack = root/'stack'
    private(stack/'.env')
    env = dict(line.split('=', 1) for line in (stack/'.env').read_text().splitlines()
               if line and not line.startswith('#') and '=' in line)
    project = env.get('HONUA_COMPOSE_PROJECT', '')
    if not re.fullmatch(r'honua-local-[a-f0-9]{24}', project):
        raise RuntimeError('cleanup refuses an unowned compose project')
    if not (stack/'compose.yaml').is_file() or (stack/'compose.yaml').is_symlink():
        raise RuntimeError('compose file is not a regular installer output')
    return ['docker', 'compose', '--project-name', project, '--env-file', str(stack/'.env'),
            '--file', str(stack/'compose.yaml')]


def image_identity(image):
    """Optional human-readable tags do not alter an immutable repository/digest identity."""
    if not isinstance(image, str) or image.count('@') != 1:
        raise RuntimeError('server image requires an immutable repository and digest')
    repository, image_digest = image.split('@')
    if not re.fullmatch('sha256:[0-9a-f]{64}', image_digest):
        raise RuntimeError('server image requires an immutable repository and digest')
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    if not re.fullmatch(r'[a-z0-9][a-z0-9.:-]*(?:/[a-z0-9][a-z0-9._-]*)+', repository):
        raise RuntimeError('server image repository is malformed')
    return repository + '@' + image_digest


def validate_bindings(platform, setup, manifest_digest):
    if setup.get('candidateId') != 'manifest-sha256:' + manifest_digest:
        raise RuntimeError('catalog manifest is not bound to this platform manifest')
    server = platform['components']['honua-server']
    if not re.fullmatch('sha256:[0-9a-f]{64}', server['digest']):
        raise RuntimeError('server image requires immutable digest')
    if not re.fullmatch('[0-9a-f]{40}', server['sha']):
        raise RuntimeError('server source requires immutable revision')
    image_name = server['image'].split('@')[0]
    if ':' in image_name.rsplit('/', 1)[-1]:
        image_name = image_name.rsplit(':', 1)[0]
    if image_identity(setup['serverImage']) != image_identity(image_name + '@' + server['digest']):
        raise RuntimeError('setup/platform image digest disagreement')
    packages = []
    for name in ['honua-sdk-js', 'honua-mcp-server']:
        pin = platform['clientArtifacts'][name]
        if not re.fullmatch('[0-9a-f]{40}', pin['sourceSha']):
            raise RuntimeError('package source SHA is not immutable')
        packages.append({'name': pin['package'], 'version': pin['version'], 'integrity': pin['integrity']})
    if sorted(setup['packages'], key=lambda x:x['name']) != sorted(packages, key=lambda x:x['name']):
        raise RuntimeError('setup/platform client integrity disagreement')
    return server, packages


def run(args, *, env=None, cwd=None, timeout=900, log):
    with log.open('wb') as out:
        result = subprocess.run(args, env=env, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    log.chmod(0o600)
    if result.returncode:
        raise RuntimeError('command failed; private log retained')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['prepare', 'install', 'restart', 'image', 'fixture',
                                           'parity', 'qualify', 'capture', 'cleanup', 'report'], required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--platform-manifest', type=Path)
    parser.add_argument('--platform-sha256')
    parser.add_argument('--setup-manifest', type=Path)
    parser.add_argument('--setup-sha256')
    parser.add_argument('--release-checkout', type=Path)
    parser.add_argument('--release-commit')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    if args.stage == 'report':
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        qualification = json.loads((root/'qualification.json').read_text()) if (root/'qualification.json').exists() else {}
        cleanup = json.loads((root/'cleanup.json').read_text()) if (root/'cleanup.json').exists() else {}
        required = ['state.json', 'image.json', 'restart.json', 'fixture.json', 'setup-catalog-receipt.json',
                    'qualification.json', 'rendered.png', 'cleanup.json', 'bounded-evidence-index.json']
        complete = all((root/name).is_file() for name in required)
        passed = complete and qualification.get('pass') is True and cleanup.get('pass') is True
        write(root/'report.json', {'schemaVersion':'honua.sdk-local-setup-hosted/v1',
            'scope':'local-setup+bounded-discovery+style-render', 'pass':passed,
            'missing':[name for name in required if not (root/name).is_file()],
            'receipts':{name:digest(root/name) for name in required if (root/name).is_file()},
            'remaining':['release123 deterministic journey consumption', 'release129 AWS wrapper',
                         'release161 genuine model and separate human approval', 'release certification lock']})
        if not passed:
            raise RuntimeError('bounded proof incomplete or cleanup failed')
        return
    if args.stage == 'cleanup':
        if not root.exists():
            return
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise RuntimeError('cleanup root is not private and owned')
        if (root/'stack/.env').exists():
            run([*compose_args(root), 'down', '--volumes', '--remove-orphans'],
                timeout=180, log=root/'private-cleanup.log')
        write(root/'cleanup.json', {'pass':True, 'scope':'owned installer project only'})
        return
    if args.stage == 'prepare':
        if root.exists():
            raise RuntimeError('prepare refuses an existing root')
        for key in ['platform_manifest', 'platform_sha256', 'setup_manifest', 'setup_sha256',
                    'release_checkout', 'release_commit']:
            if getattr(args, key) is None:
                raise RuntimeError('prepare requires all independent manifest/source pins')
        release = args.release_checkout.resolve()
        actual = subprocess.check_output(['git', '-C', str(release), 'rev-parse', 'HEAD'], text=True).strip()
        if not re.fullmatch('[0-9a-f]{40}', args.release_commit) or actual != args.release_commit:
            raise RuntimeError('release checkout commit mismatch')
        subprocess.run(['git', '-C', str(release), 'diff', '--exit-code', 'HEAD', '--',
                        'certification/terminal-journey/pins.py', 'tools/verify_client_artifacts.py'],
                       stdout=subprocess.DEVNULL, check=True)
        platform_text = checked_bytes(args.platform_manifest, args.platform_sha256)
        setup_text = checked_bytes(args.setup_manifest, args.setup_sha256)
        platform = yaml.safe_load(platform_text)
        setup = json.loads(setup_text)
        setup_location = args.setup_manifest.resolve()
        if not setup_location.is_relative_to(release/'certification/sdk-local-setup/manifests'):
            raise RuntimeError('setup manifest must be a reviewed file in the designated manifest directory')
        subprocess.run(['git', '-C', str(release), 'ls-files', '--error-unmatch', str(setup_location)],
                       stdout=subprocess.DEVNULL, check=True)
        subprocess.run(['git', '-C', str(release), 'diff', '--exit-code', 'HEAD', '--', str(setup_location)],
                       stdout=subprocess.DEVNULL, check=True)
        server, expected_packages = validate_bindings(platform, setup, args.platform_sha256)
        root.mkdir(mode=0o700)
        (root/'platform-manifest.yaml').write_text(platform_text)
        (root/'setup-manifest.json').write_text(setup_text)
        pins = load_pins(release)
        # The canonical verifier binds npm provenance/source as well as tarball SRI.
        selected = {**platform, 'clientArtifacts':{name:platform['clientArtifacts'][name]
                    for name in ['honua-sdk-js', 'honua-mcp-server']}}
        verified = pins._load_verifier().verify_manifest(selected, include_identities=True)
        workspace = pins.resolve_client_workspace(platform, root/'verified-tarballs')
        if workspace.status != 'pass':
            raise RuntimeError('published bytes verification failed')
        for row in workspace.resolved:
            if verified[row.name]['sha256'] != 'sha256:' + row.tarball_sha256:
                raise RuntimeError('resolved tarball differs from provenance-verified bytes')
        consumer = root/'consumer'
        consumer.mkdir()
        write(consumer/'package.json', {'name':'honua-sdk1401-replay', 'private':True})
        # Intentionally DO NOT call install_executables: its historical prerelease
        # ERESOLVE fallback bypasses default peer resolution. There is one attempt.
        run(['npm','install','--no-audit','--no-fund','--cache',str(root/'npm-cache'),
             *[str(row.tarball) for row in workspace.resolved], '--ignore-scripts'], cwd=consumer, env=isolated_env(root),
            log=root/'private-npm-install.log')
        sdk = consumer/'node_modules/@honua/sdk-js'
        mcp = consumer/'node_modules/@honua/mcp-server'
        for base, expected in [(sdk, expected_packages[0]), (mcp, expected_packages[1])]:
            pkg = json.loads((base/'package.json').read_text())
            if pkg['name'] != expected['name'] or pkg['version'] != expected['version']:
                raise RuntimeError('installed package identity mismatch')
        source = sdk/'dist/src/local-install.js'
        code = 'const m=await import(process.argv[1]);console.log(JSON.stringify({image:m.LOCAL_INSTALL_SERVER_IMAGE,mcp:m.LOCAL_INSTALL_MCP_PACKAGE}));'
        installed = json.loads(subprocess.check_output(['node','--input-type=module','-e',code,source.as_uri()], text=True))
        if image_identity(installed['image']) != image_identity(setup['serverImage']):
            raise RuntimeError('published installer pin differs from reviewed candidate; no image override allowed')
        if installed['image'] != setup['serverImage']:
            raise RuntimeError('reviewed setup manifest must preserve the exact shipped image string for published qualifier equality')
        if installed['mcp'] != '@honua/mcp-server@' + expected_packages[1]['version']:
            raise RuntimeError('generated MCP client pin differs from reviewed installed pair')
        for name in ['fixture.mjs', 'request.mjs']:
            shutil.copyfile(HERE/name, consumer/name)
        write(root/'state.json', {'schemaVersion':'honua.sdk1401-replay-preparation/v1',
            'platformSha256':args.platform_sha256, 'setupSha256':args.setup_sha256,
            'releaseCommit':actual, 'releaseVerifierSha256':digest(release/'tools/verify_client_artifacts.py'),
            'releasePinsSha256':digest(release/'certification/terminal-journey/pins.py'),
            'producerSha256':digest(Path(__file__)),
            'workflowRun':os.environ.get('GITHUB_SERVER_URL', '') + '/' + os.environ.get('GITHUB_REPOSITORY', '')
                + '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', ''),
            'serverSourceSha':server['sha'], 'serverImage':installed['image'],
            'candidateImageIdentity':image_identity(setup['serverImage']),
            'clientPins':pins.receipt_pins(platform), 'resolved':[r.as_receipt() for r in workspace.resolved],
            'publishedSourceVerification':verified, 'defaultPeerResolution':True,
            'fixtureProducerSha256':digest(HERE/'fixture.mjs'),
            'requestProducerSha256':digest(HERE/'request.mjs'),
            'scope':'bounded-setup-style-only', 'port':19819})
        print('PREPARED: verified published pair and exact installer pin; no server started')
        return
    if not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise RuntimeError('replay root is not private and owned')
    state = json.loads((root/'state.json').read_text())
    checked_bytes(root/'platform-manifest.yaml', state['platformSha256'])
    checked_bytes(root/'setup-manifest.json', state['setupSha256'])
    consumer = root/'consumer'
    honua = consumer/'node_modules/.bin/honua'
    stack = root/'stack'
    if args.stage in ['install', 'restart']:
        if args.stage == 'install':
            if stack.exists():
                raise RuntimeError('initial install refuses an existing stack directory')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',state['port']))
        else:
            private(stack/'.env')
            before = digest(stack/'.mcp.json')
            run([*compose_args(root), 'restart', 'honua'], cwd=consumer, timeout=90,
                log=root/'private-container-restart.log')
        install_env = isolated_env(root)
        run([str(honua),'admin','install','local','--profile','gp-dev','--directory',str(stack),
             '--http-port',str(state['port']),'--yes'], cwd=consumer, timeout=600,
            env=install_env,
            log=root/('private-'+args.stage+'.json'))
        if args.stage == 'restart':
            if before != digest(stack/'.mcp.json'):
                raise RuntimeError('idempotent restart replaced the generated credential/configuration')
            write(root/'restart.json', {'pass':True, 'generatedConfigurationUnchanged':True})
        print('INSTALL READY: private installer receipt retained; candidate qualification still pending')
        return
    private(stack/'.mcp.json')
    config = json.loads((stack/'.mcp.json').read_text())['mcpServers']['honua']['env']
    if config['HONUA_MCP_REMOTE_URL'] != f'http://127.0.0.1:{state["port"]}/mcp':
        raise RuntimeError('generated proxy endpoint differs from owned install')
    env = isolated_env(root)
    env.update(config)
    if args.stage == 'image':
        image = json.loads(subprocess.check_output(['docker','image','inspect',state['serverImage']], text=True))[0]
        revision = (image['Config'].get('Labels') or {}).get('org.opencontainers.image.revision')
        if revision != state['serverSourceSha'] or state['candidateImageIdentity'] not in image['RepoDigests']:
            raise RuntimeError('image source/digest does not match reviewed candidate')
        container_id = subprocess.check_output([*compose_args(root),'ps','-q','honua'], text=True).strip()
        if not re.fullmatch('[0-9a-f]{12,64}', container_id):
            raise RuntimeError('owned server container is missing or ambiguous')
        container = json.loads(subprocess.check_output(['docker','inspect',container_id], text=True))[0]
        if container['Image'] != image['Id'] or container['Config']['Image'] != state['serverImage']:
            raise RuntimeError('running container does not use the verified immutable image')
        runtime = dict(value.split('=',1) for value in container['Config']['Env'] if '=' in value)
        if runtime.get('HONUA_GIT_SHA') != state['serverSourceSha']:
            raise RuntimeError('running source identity differs from candidate')
        write(root/'image.json', {'pass':True,'serverImage':state['serverImage'],
            'candidateImageIdentity':state['candidateImageIdentity'],
            'sourceSha':revision,'runningSourceSha':runtime['HONUA_GIT_SHA'],'imageId':image['Id']})
        return
    if args.stage in ['fixture', 'capture']:
        run(['node',str(consumer/'fixture.mjs'),args.stage,str(root)], env=env, cwd=consumer,
            timeout=180,log=root/('private-'+args.stage+'.log'))
        return
    if args.stage == 'parity':
        run(['node',str(consumer/'node_modules/@honua/mcp-server/dist/src/certification/setup-parity.js'),
             str(root/'setup-manifest.json'),state['setupSha256'],str(root/'setup-catalog-receipt.json')],
            env=env,cwd=consumer,timeout=180,log=root/'private-parity.log')
        print('CATALOG PASS: actual HTTP/installed stdio parity receipt retained; style proof pending')
        return
    fixture = json.loads((root/'fixture.json').read_text())
    run([str(honua),'admin','qualify','--directory',str(stack),
         '--manifest',str(root/'setup-manifest.json'),'--manifest-sha256',state['setupSha256'],
         '--catalog-receipt',str(root/'setup-catalog-receipt.json'),
         '--service-id',fixture['serviceId'],'--layer-id',str(fixture['layerId']),
         '--bbox',','.join(map(str,fixture['bbox'])),
         '--yes','--output',str(root/'qualification.json')],env=env,cwd=consumer,timeout=180,
        log=root/'private-qualification.log')
    receipt=json.loads((root/'qualification.json').read_text())
    if receipt.get('pass') is not True:
        raise RuntimeError('bounded qualification did not pass')
    if receipt.get('styleRender', {}).get('styleId') != fixture['styleId']:
        raise RuntimeError('rendered preset is not the intended fixture style')
    write(root/'bounded-evidence-index.json', {'scope':'bounded-setup-style-only',
        'candidateId':'manifest-sha256:'+state['platformSha256'], 'pins':state,
        'receipts':{name:digest(root/name) for name in ['setup-catalog-receipt.json','qualification.json']},
        'remaining':['release123 full deterministic journey','release129 AWS wrapper',
                     'release161 real model and separate human approval','release certification lock']})
    print('BOUNDED PASS: independent release journey and consumer qualification remain')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Commands may carry private response data. Never echo exception strings,
        # subprocess output, environment, generated MCP config, or credentials.
        reason = str(error) if type(error) is RuntimeError else 'replay stage failed; inspect its private command log'
        print('BLOCKED: ' + reason + '. No fallback attempted.', file=sys.stderr)
        sys.exit(1)
