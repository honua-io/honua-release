"""Fail-closed input, ownership, resolver and evidence controls; no live qualification claim."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('sdk_local_setup', HERE/'run.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def inputs():
    packages = {
        'honua-sdk-js': {'package':'@honua/sdk-js', 'version':'0.1.16', 'sourceSha':'a'*40, 'integrity':'sha512-'+'A'*86+'=='},
        'honua-mcp-server': {'package':'@honua/mcp-server', 'version':'0.1.16', 'sourceSha':'b'*40, 'integrity':'sha512-'+'B'*86+'=='},
    }
    platform = {'components':{'honua-server':{'sha':'c'*40,'digest':'sha256:'+'d'*64,'image':'ghcr.io/honua-io/honua-server:nightly-ccccccc'}},'clientArtifacts':packages}
    setup = {'candidateId':'manifest-sha256:'+'e'*64, 'serverImage':'ghcr.io/honua-io/honua-server@sha256:'+'d'*64,
             'packages':[{'name':p['package'],'version':p['version'],'integrity':p['integrity']} for p in packages.values()]}
    return platform, setup


class BindingsTests(unittest.TestCase):
    def test_exact_independently_bound_pair(self):
        platform, setup = inputs()
        server, packages = replay.validate_bindings(platform, setup, 'e'*64)
        self.assertEqual(server['sha'], 'c'*40)
        self.assertEqual([p['name'] for p in packages], ['@honua/sdk-js','@honua/mcp-server'])

    def test_tagged_immutable_image_preserves_identity(self):
        platform, setup = inputs()
        setup['serverImage'] = 'ghcr.io/honua-io/honua-server:nightly-ccccccc@sha256:'+'d'*64
        replay.validate_bindings(platform, setup, 'e'*64)
        self.assertEqual(replay.image_identity(setup['serverImage']), 'ghcr.io/honua-io/honua-server@sha256:'+'d'*64)
        setup['serverImage'] = setup['serverImage'].replace('honua-server:', 'unrelated:')
        with self.assertRaisesRegex(RuntimeError, 'disagreement'):
            replay.validate_bindings(platform, setup, 'e'*64)

    def test_wrong_platform_candidate_refused(self):
        platform, setup = inputs()
        with self.assertRaisesRegex(RuntimeError, 'bound'):
            replay.validate_bindings(platform, setup, 'f'*64)

    def test_floating_or_mismatched_image_refused(self):
        for image in ['ghcr.io/honua-io/honua-server:nightly', 'ghcr.io/honua-io/honua-server@sha256:'+'f'*64]:
            platform, setup = inputs()
            setup['serverImage'] = image
            with self.assertRaises(RuntimeError):
                replay.validate_bindings(platform, setup, 'e'*64)

    def test_mutable_server_or_package_source_refused(self):
        for target in ['server','sdk','mcp']:
            platform, setup = inputs()
            if target == 'server':
                platform['components']['honua-server']['sha'] = 'trunk'
            else:
                platform['clientArtifacts']['honua-sdk-js' if target=='sdk' else 'honua-mcp-server']['sourceSha'] = 'trunk'
            with self.assertRaises(RuntimeError):
                replay.validate_bindings(platform, setup, 'e'*64)

    def test_mismatched_missing_or_extra_package_pin_refused(self):
        _, expected = inputs()
        variants = [expected['packages'][:1], [*expected['packages'], copy.deepcopy(expected['packages'][0])]]
        changed = copy.deepcopy(expected['packages'])
        changed[1]['integrity'] = 'sha512-'+'C'*86+'=='
        variants.append(changed)
        for packages in variants:
            platform, setup = inputs()
            setup['packages'] = packages
            with self.assertRaisesRegex(RuntimeError, 'client integrity'):
                replay.validate_bindings(platform, setup, 'e'*64)

    def test_digest_mismatch_cannot_read_as_reviewed(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'manifest'
            file.write_text('{}')
            with self.assertRaises(RuntimeError):
                replay.checked_bytes(file, '0'*64)
            self.assertEqual(replay.checked_bytes(file, hashlib.sha256(b'{}').hexdigest()), '{}')


class OwnershipAndResolverTests(unittest.TestCase):
    def test_private_default_resolver_discards_relaxations_and_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = replay.isolated_env(root, {'PATH':'/tools', 'NPM_CONFIG_FORCE':'true',
                'npm_config_legacy_peer_deps':'true', 'NPM_CONFIG_REGISTRY':'https://untrusted.test',
                'AWS_SECRET_ACCESS_KEY':'private-cloud', 'GITHUB_TOKEN':'private-github',
                'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'private-oidc', 'ARBITRARY_SECRET':'private-other',
                'HONUA_SERVER_IMAGE':'unrelated', 'HONUA_ADMIN_KEY':'private-control'})
            self.assertEqual(env['PATH'], '/tools')
            self.assertFalse(any(key in env for key in ['AWS_SECRET_ACCESS_KEY','GITHUB_TOKEN',
                'ACTIONS_ID_TOKEN_REQUEST_TOKEN','ARBITRARY_SECRET']))
            self.assertFalse(any('force' in key or 'legacy_peer' in key for key in env))
            self.assertFalse(any(key.startswith('HONUA_') for key in env))
            self.assertEqual(env['npm_config_registry'], 'https://registry.npmjs.org')
            self.assertEqual(env['npm_config_cache'], str(root/'npm-cache'))
            self.assertNotEqual(env['npm_config_userconfig'], env['npm_config_globalconfig'])
            self.assertEqual(Path(env['npm_config_userconfig']).read_text(), '')

    def test_published_module_inspection_also_uses_candidate_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY':'private-cloud', 'GH_TOKEN':'private-token'}):
                with patch.object(replay.subprocess, 'check_output', return_value='{"image":"immutable","mcp":"exact"}') as call:
                    self.assertEqual(replay.published_installer_pins(root/'published.js', root)['mcp'], 'exact')
                    self.assertNotIn('AWS_SECRET_ACCESS_KEY', call.call_args.kwargs['env'])
                    self.assertNotIn('GH_TOKEN', call.call_args.kwargs['env'])
                    self.assertEqual(call.call_args.args[0][-1], (root/'published.js').as_uri())

    def test_compose_cleanup_only_random_owned_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack = root/'stack'
            stack.mkdir()
            (stack/'compose.yaml').write_text('services: {}')
            for project in ['honua-local', 'unrelated-project', 'honua-local-'+'0'*24]:
                replay.write(stack/'.env', {})
                (stack/'.env').write_text('HONUA_COMPOSE_PROJECT='+project+'\n')
                if project.endswith('0'*24):
                    args = replay.compose_args(root)
                    self.assertEqual(args[:4], ['docker','compose','--project-name',project])
                    self.assertIn(str(stack/'compose.yaml'), args)
                else:
                    with self.assertRaises(RuntimeError):
                        replay.compose_args(root)

    def test_symlink_compose_and_readable_credentials_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stack = root/'stack'
            stack.mkdir()
            target = root/'unrelated-compose.yaml'
            target.write_text('services: {}')
            (stack/'compose.yaml').symlink_to(target)
            replay.write(stack/'.env', {})
            (stack/'.env').write_text('HONUA_COMPOSE_PROJECT=honua-local-'+'0'*24+'\n')
            with self.assertRaises(RuntimeError):
                replay.compose_args(root)
            (stack/'.env').chmod(0o644)
            with self.assertRaises(RuntimeError):
                replay.private(stack/'.env')

    def test_failed_command_is_not_retried_or_relaxed(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'private.log'
            with patch.object(replay.subprocess, 'run') as call:
                call.return_value.returncode = 1
                with self.assertRaises(RuntimeError):
                    replay.run(['npm','install','exact-package'], log=log)
                call.assert_called_once()
                self.assertEqual(call.call_args.args[0], ['npm','install','exact-package'])
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_incomplete_or_failed_cleanup_never_reports_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            replay.write(root/'qualification.json', {'pass':True})
            replay.write(root/'cleanup.json', {'pass':False})
            with patch('sys.argv', ['run.py','--stage','report','--root',str(root)]):
                with self.assertRaises(RuntimeError):
                    replay.main()
            report = json.loads((root/'report.json').read_text())
            self.assertFalse(report['pass'])
            self.assertIn('rendered.png', report['missing'])
            self.assertNotIn('private', json.dumps(report))

    def test_workflow_only_uploads_public_allowlist_and_always_cleans(self):
        import yaml
        workflow = yaml.safe_load((HERE.parents[1]/'.github/workflows/sdk-local-setup.yml').read_text())
        job = workflow['jobs']['installed']
        self.assertIn('inputs.execute', job['if'])
        cleanup = [s for s in job['steps'] if '--stage cleanup' in s.get('run','')]
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(cleanup[0]['if'], 'always()')
        bounded = [step['timeout-minutes'] for step in job['steps'] if 'timeout-minutes' in step]
        self.assertLessEqual(sum(bounded), job['timeout-minutes'] - 7)
        upload = [s for s in job['steps'] if s.get('uses','').startswith('actions/upload-artifact@')][0]
        for forbidden in ['private', '.env', '.mcp.json', 'consumer', '**']:
            self.assertNotIn(forbidden, upload['with']['path'])
        self.assertIn('/report.json', upload['with']['path'])


if __name__ == '__main__':
    unittest.main()
