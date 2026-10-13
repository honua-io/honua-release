"""Offline expectation refusal controls; no server or installed qualification claim."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('source_catalog', Path(__file__).with_name('export_catalog.py'))
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def rebind(value):
    value['descriptorJsons'] = [json.dumps(tool, separators=(',', ':')) for tool in value['tools']]
    value['descriptorJson'] = '[' + ','.join(value['descriptorJsons']) + ']'
    value['descriptorBytes'] = len(value['descriptorJson'].encode())
    value['largestDescriptorBytes'] = max(len(item.encode()) for item in value['descriptorJsons'])
    for raw, hashed in [('revisionCanonical', 'revisionDigest'), ('membershipCanonical', 'membershipDigest'),
                        ('descriptorJson', 'descriptorDigest')]:
        value[hashed] = 'sha256:' + export.digest(value[raw].encode())
    return value


def projection():
    return rebind({'schemaVersion':'honua.setup-source-projection/v1', 'view':'setup', 'revision':'setup.test',
        'revisionCanonical':'setup.test\n', 'membershipCanonical':'readiness/honua_test\n',
        'tools':[{'name':'honua_test', 'description':'Read only control',
                  'inputSchema':{'type':'object', 'properties':{'name':{'type':'string'}}},
                  'outputSchema':{'type':'object', 'required':['status'], 'properties':{'status':{'type':'string'}}},
                  'annotations':{'readOnlyHint':True}, '_meta':{'honua.studio':{'revision':'setup.test'}}}],
        'members':[{'name':'honua_test', 'stage':'readiness', 'isDynamic':False}], 'stages':['readiness'],
        'budgets':{'maxDescriptors':48, 'maxAggregateBytes':131072, 'maxDescriptorBytes':16384},
        'serverInfo':{'name':'honua.operator.mcp', 'version':'v1'}})


def platform():
    return {'components':{'honua-server':{'sha':'a'*40, 'digest':'sha256:'+'b'*64,
             'image':'ghcr.io/honua-io/honua-server:reviewed'}},
            'clientArtifacts':{key:{'package':name, 'version':'0.2.0', 'sourceSha':'c'*40,
                 'integrity':'sha512-'+'A'*86+'=='} for key, name in
                 [('honua-sdk-js','@honua/sdk-js'), ('honua-mcp-server','@honua/mcp-server')]}}


class ProjectionTests(unittest.TestCase):
    def test_complete_source_projection_preserves_nested_schemas_annotations_and_metadata(self):
        value = projection()
        actual = export.validate_projection(value)
        self.assertEqual(actual['tools'], value['tools'])
        self.assertNotIn('revisionCanonical', actual)
        self.assertEqual(export.validate_projection(copy.deepcopy(value)), actual)

    def test_each_digest_is_verified_against_original_source_serialization(self):
        for key in ['revisionDigest', 'membershipDigest', 'descriptorDigest']:
            with self.subTest(key=key):
                value = projection()
                value[key] = 'sha256:' + '0'*64
                with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                    export.validate_projection(value)

    def test_schema_and_annotations_refusals_survive_rebound_digests(self):
        for field, invalid in [('inputSchema', {'type':'array'}), ('outputSchema', None),
                               ('inputSchema', {'type':'object', 'properties':{'x':{'type':'not-a-json-type'}}}),
                               ('annotations', {}), ('description', '')]:
            with self.subTest(field=field):
                value = projection()
                value['tools'][0][field] = invalid
                rebind(value)
                with self.assertRaises(ValueError):
                    export.validate_projection(value)

    def test_wire_projection_cannot_drop_nested_schema_fields(self):
        value = projection()
        del value['tools'][0]['inputSchema']['properties']
        with self.assertRaisesRegex(ValueError, 'wire bytes disagree'):
            export.validate_projection(value)

    def test_duplicate_or_reordered_roster_and_dynamic_members_fail(self):
        for mode in ['duplicate', 'rename', 'dynamic', 'unknown-stage', 'empty-stage']:
            with self.subTest(mode=mode):
                value = projection()
                if mode == 'duplicate':
                    value['tools'].append(copy.deepcopy(value['tools'][0]))
                    value['members'].append(copy.deepcopy(value['members'][0]))
                    value['membershipCanonical'] *= 2
                elif mode == 'rename':
                    value['members'][0]['name'] = 'different'
                elif mode == 'dynamic':
                    value['members'][0]['isDynamic'] = True
                elif mode == 'unknown-stage':
                    value['members'][0]['stage'] = 'different'
                else:
                    value['stages'].append('missing')
                rebind(value)
                with self.assertRaises(ValueError):
                    export.validate_projection(value)

    def test_budget_and_measured_byte_mismatches_fail(self):
        for mode in ['aggregate', 'individual', 'measured', 'largest', 'boolean']:
            with self.subTest(mode=mode):
                value = projection()
                if mode == 'aggregate': value['budgets']['maxAggregateBytes'] = 1
                elif mode == 'individual': value['budgets']['maxDescriptorBytes'] = 1
                elif mode == 'measured': value['descriptorBytes'] += 1
                elif mode == 'largest': value['largestDescriptorBytes'] -= 1
                else: value['budgets']['maxDescriptors'] = True
                with self.assertRaisesRegex(ValueError, 'budget'):
                    export.validate_projection(value)


class SourceAndCandidateTests(unittest.TestCase):
    def test_candidate_package_identity_and_exact_shipped_tag_are_bound(self):
        image = 'ghcr.io/honua-io/honua-server:reviewed@sha256:' + 'b'*64
        result = export.compose_manifest(projection(), platform(), 'd'*64, 'a'*40, image)
        self.assertEqual(result['serverImage'], image)
        self.assertEqual(result['candidateId'], 'manifest-sha256:'+'d'*64)
        self.assertEqual(len(result['packages']), 2)

    def test_wrong_source_image_and_package_pins_fail(self):
        for mode in ['source', 'image', 'package', 'integrity', 'version']:
            with self.subTest(mode=mode):
                manifest = platform()
                image = 'ghcr.io/honua-io/honua-server@sha256:'+'b'*64
                if mode == 'source': manifest['components']['honua-server']['sha'] = 'e'*40
                elif mode == 'image': image = image.replace('b'*64, 'e'*64)
                else: manifest['clientArtifacts']['honua-sdk-js'][mode] = 'wrong'
                with self.assertRaises((ValueError, RuntimeError)):
                    export.compose_manifest(projection(), manifest, 'd'*64, 'a'*40, image)

    def test_source_must_be_clean_exact_and_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/export.PROJECT).parent.mkdir(parents=True)
            (root/export.PROJECT).touch()
            good = ['a'*40, '', 'https://github.com/honua-io/honua-server.git']
            with patch.object(export, 'git', side_effect=good):
                export.check_server_source(root, 'a'*40)
            for responses in [['b'*40], ['a'*40, ' M source.cs'], ['a'*40, '', 'https://foreign.test/repo']]:
                with self.subTest(responses=responses), patch.object(export, 'git', side_effect=responses):
                    with self.assertRaises(ValueError):
                        export.check_server_source(root, 'a'*40)


if __name__ == '__main__':
    unittest.main()
