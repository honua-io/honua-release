"""Tests for the changed-content vendor terminology guard."""
from __future__ import annotations

import json
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

import vendor_terms_delta as guard


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(root), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


def repo(tmp_path: Path) -> Path:
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    return tmp_path


def commit(root: Path, text: str, path: str = 'source.cs') -> str:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding='utf-8')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fixture')
    return git(root, 'rev-parse', 'HEAD')


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp_path = Path(self.temp.name)

    def test_existing_hits_do_not_block_unrelated_additions(self):
        root = repo(self.tmp_path)
        mark = 'Es' + 'ri'
        base = commit(root, f'// {mark} adapter\nint value = 1;\n')
        head = commit(root, f'// {mark} adapter\nint value = 1;\nint next = 2;\n')
        assert guard.find_new_hits(root, 'fixture', base, head) == []

    def test_new_avoidable_word_is_rejected(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        head = commit(root, 'int value = 1;\n// ' + 'Es' + 'ri adapter\n')
        assert any(h.cls == 'avoidable' for h in guard.find_new_hits(root, 'fixture', base, head))

    def test_protocol_identifier_and_srid_name_are_allowed(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        head = commit(root, 'int value = 1;\nstring value = "' + 'es' + 'ri' + 'GeometryPoint";\nint sourceSrid = 1;\n')
        assert guard.find_new_hits(root, 'fixture', base, head) == []

    def test_mark_in_new_filename_is_rejected(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        head = commit(root, 'int value = 1;\n', 'new' + 'Es' + 'ri' + 'Adapter.cs')
        assert any(h.line == 0 for h in guard.find_new_hits(root, 'fixture', base, head))

    def test_replacing_one_old_use_with_another_does_not_hide_a_new_line(self):
        root = repo(self.tmp_path)
        mark = 'Es' + 'ri'
        base = commit(root, f'// {mark} old\nint value = 1;\n')
        head = commit(root, f'int value = 1;\n// {mark} new\n')
        self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_invalid_base_fails_closed(self):
        root = repo(self.tmp_path)
        head = commit(root, 'int value = 1;\n')
        with self.assertRaises(subprocess.CalledProcessError):
            guard.find_new_hits(root, 'fixture', 'does-not-exist', head)

    def test_text_marked_binary_by_attributes_fails_closed(self):
        root = repo(self.tmp_path)
        base = commit(root, '*.cs binary\n', '.gitattributes')
        head = commit(root, '// ' + 'Es' + 'ri adapter\n')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_binary_source_file_fails_closed(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        (root / 'source.cs').write_bytes(b'// hidden\0' + b'Es' + b'ri')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'binary fixture')
        head = git(root, 'rev-parse', 'HEAD')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_annotation_path_escapes_workflow_command_metacharacters(self):
        self.assertEqual(guard.annotation_property('file,part:\n%'), 'file%2Cpart%3A%0A%25')

    def test_changed_submodule_fails_closed(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        git(root, 'update-index', '--add', '--cacheinfo', f'160000,{base},external')
        git(root, 'commit', '-qm', 'add submodule')
        head = git(root, 'rev-parse', 'HEAD')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_pull_request_title_and_body_are_classified(self):
        payload = {'pull_request': {'title': 'Feature for ' + 'Es' + 'ri',
                                    'body': 'Use ' + 'Arc' + 'GIS client'}}
        hits = guard.find_pr_metadata_hits('fixture', payload)
        self.assertEqual({h.path for h in hits}, {'pull-request-title.md', 'pull-request-body.md'})

    def test_pull_request_protocol_identifier_is_allowed(self):
        payload = {'pull_request': {'title': 'Handle ' + 'es' + 'ri' + 'GeometryPoint', 'body': None}}
        self.assertEqual(guard.find_pr_metadata_hits('fixture', payload), [])

    def test_invalid_pull_request_payload_fails_closed(self):
        with self.assertRaises(RuntimeError):
            guard.find_pr_metadata_hits('fixture', {})

    def test_pathspec_metacharacters_do_not_hide_changes(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n', 'source[1].cs')
        head = commit(root, '// ' + 'Es' + 'ri adapter\n', 'source[1].cs')
        self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_changed_existing_file_with_unchanged_marked_directory_is_allowed(self):
        root = repo(self.tmp_path)
        path = 'old' + 'Es' + 'ri' + '/source.cs'
        base = commit(root, 'int value = 1;\n', path)
        head = commit(root, 'int value = 2;\n', path)
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, head), [])

    def test_non_string_pull_request_body_fails_closed(self):
        with self.assertRaises(RuntimeError):
            guard.find_pr_metadata_hits('fixture', {'pull_request': {'title': 'Change', 'body': 0}})

    def test_cli_fails_closed_without_pull_request_event(self):
        root = repo(self.tmp_path)
        head = commit(root, 'int value = 1;\n')
        cli = Path(guard.__file__)
        result = subprocess.run([sys.executable, str(cli), '--root', str(root), '--repo', 'fixture',
                                 '--base', head, '--head', head, '--event-name', 'pull_request'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_utf16_powershell_cannot_bypass_the_guard(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        (root / 'run.ps1').write_bytes(('Write-Output "' + 'Es' + 'ri"').encode('utf-16'))
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'encoded script')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD'))

    def test_known_binary_asset_is_not_mistaken_for_source(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        (root / 'map.png').write_bytes(b'\x89PNG\r\n\x1a\n\0\x01\x02')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'image asset')
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD')), [])

    def test_non_lf_line_separator_cannot_hide_a_new_mark(self):
        for separator in ('\r', '\u2028', '\u0085'):
            with self.subTest(separator=repr(separator)), tempfile.TemporaryDirectory() as directory:
                root = repo(Path(directory))
                base = commit(root, 'safe\n')
                head = commit(root, 'safe' + separator + '# ' + 'Es' + 'ri\n')
                self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_new_mark_in_markdown_fence_label_is_rejected(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n', 'notes.md')
        head = commit(root, 'baseline\n```' + 'Es' + 'ri\nexample\n```\n', 'notes.md')
        self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_long_identifier_cannot_hide_a_visible_mark(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, 'identifier = "' + 'A' * 90 + 'Es' + 'ri' + 'A' * 90 + '"\n')
        self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_pull_request_markdown_fence_label_is_checked(self):
        payload = {'pull_request': {'title': 'Change', 'body': '```' + 'Es' + 'ri\nexample\n```'}}
        self.assertTrue(guard.find_pr_metadata_hits('fixture', payload))

    def test_script_disguised_as_binary_asset_fails_closed(self):
        root = repo(self.tmp_path)
        base = commit(root, 'int value = 1;\n')
        (root / 'script.png').write_bytes(b'#!/bin/sh\n# \0 Es' + b'ri\necho executed\n')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'disguised script')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD'))

    def test_lowercase_compound_product_name_is_rejected_in_code_and_paths(self):
        for path in ('source.cs', 'my' + 'arc' + 'gisclient/file.cs'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                root = repo(Path(directory))
                base = commit(root, 'baseline\n')
                head = commit(root, 'client = "my' + 'arc' + 'gisclient"\n', path)
                self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_lowercase_compound_product_name_is_rejected_in_pr_text(self):
        payload = {'pull_request': {'title': 'my' + 'arc' + 'gisclient', 'body': ''}}
        self.assertTrue(guard.find_pr_metadata_hits('fixture', payload))

    def test_binary_polyglot_with_visible_mark_is_rejected(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        (root / 'script.png').write_bytes(b'\x89PNG\r\n\x1a\n\0# ' + b'Es' + b'ri\necho executed\n')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'polyglot')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD'))

    def test_utf16_mark_in_binary_asset_is_rejected(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        (root / 'map.png').write_bytes(b'\x89PNG\r\n\x1a\n\0' + ('Es' + 'ri').encode('utf-16-le'))
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'encoded asset')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD'))

    def test_confidential_path_is_not_printed_by_cli(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        secret_path = 'private.' + 'ap' + 'rx'
        head = commit(root, 'contents\n', secret_path)
        result = subprocess.run([sys.executable, guard.__file__, '--root', str(root), '--repo', 'fixture',
                                 '--base', base, '--head', head], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(secret_path, result.stdout + result.stderr)
        self.assertIn('withheld', result.stdout)

    def test_uninspectable_confidential_path_is_not_printed_by_cli(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        secret_path = 'private.' + 'ap' + 'rx'
        (root / secret_path).write_bytes(b'\0unknown')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'binary fixture')
        head = git(root, 'rev-parse', 'HEAD')
        result = subprocess.run([sys.executable, guard.__file__, '--root', str(root), '--repo', 'fixture',
                                 '--base', base, '--head', head], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(secret_path, result.stdout + result.stderr)

    def test_lowercase_vendor_compound_is_caught_without_flagging_srid(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, 'my' + 'es' + 'ri' + 'client = 1\nsourceSrid = 2\n')
        hits = guard.find_new_hits(root, 'fixture', base, head)
        self.assertEqual([hit.line for hit in hits], [1])

    def test_geopackage_source_srid_is_not_a_vendor_mark(self):
        import sqlite3
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        with sqlite3.connect(root / 'data.gpkg') as db:
            db.execute('CREATE TABLE layers (SourceSrid INTEGER)')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'add geopackage')
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD')), [])

    def test_binary_asset_allows_srid_and_spec_identifier(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        data = b'\x89PNG\r\n\x1a\n\0' + ('SourceSrid ' + 'es' + 'riGeometryPoint').encode('utf-16-le')
        (root / 'map.png').write_bytes(data)
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'fixture')
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD')), [])

    def test_binary_non_ascii_bytes_cannot_hide_a_plain_mark(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        (root / 'map.png').write_bytes(b'\x89PNG\r\n\x1a\n\0\xff' + b'es' + b'ri\xff')
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'asset')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD'))

    def test_reusable_pull_request_target_checks_metadata(self):
        root = repo(self.tmp_path)
        head = commit(root, 'baseline\n')
        event = root / 'event.json'
        event.write_text(json.dumps({'pull_request': {'title': 'Use ' + 'Es' + 'ri', 'body': ''}}))
        result = subprocess.run([sys.executable, guard.__file__, '--root', str(root), '--repo', 'fixture',
                                 '--base', head, '--head', head, '--event-name', 'pull_request_target',
                                 '--event', str(event)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)

    def test_unsupported_event_fails_closed(self):
        root = repo(self.tmp_path)
        head = commit(root, 'baseline\n')
        result = subprocess.run([sys.executable, guard.__file__, '--root', str(root), '--repo', 'fixture',
                                 '--base', head, '--head', head, '--event-name', 'push'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_git_lfs_pointer_fails_closed(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        pointer = 'version https://git-lfs.github.com/spec/v1\noid sha256:' + 'a' * 64 + '\nsize 20\n'
        head = commit(root, pointer, 'source.py')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_html_entity_cannot_hide_rendered_name_in_markdown(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n', 'notes.md')
        head = commit(root, 'baseline\n&#69;' + 'sri\n', 'notes.md')
        self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_html_entity_cannot_hide_rendered_name_in_pr_body(self):
        payload = {'pull_request': {'title': 'Change', 'body': '&#69;' + 'sri'}}
        self.assertTrue(guard.find_pr_metadata_hits('fixture', payload))

    def test_source_escapes_cannot_hide_new_name(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        for escaped in ('\\u0045', '\\x45', '\\u{45}', '\\N{LATIN CAPITAL LETTER E}', '\\105'):
            with self.subTest(escaped=escaped):
                head = commit(root, 'const label = "' + escaped + 'sri";\n')
                self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_source_escape_keeps_allowed_wire_identifier(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, '"' + '\\u0065' + 'sriGeometryPoint"\n')
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, head), [])

    def test_paired_backslash_is_not_decoded(self):
        self.assertEqual(guard.decode_source_escapes('\\\\u0045' + 'sri'), '\\\\u0045' + 'sri')

    def test_long_backslash_run_does_not_stall(self):
        script = ('import sys; sys.path.insert(0, sys.argv[1]); import vendor_terms_delta as g; '
                  's=chr(92)*100000+"not-an-escape"; assert g.decode_source_escapes(s)==s')
        subprocess.run([sys.executable, '-c', script, str(Path(guard.__file__).parent)],
                       check=True, timeout=5)

    def test_source_escape_in_pr_description_is_detected(self):
        payload = {'pull_request': {'title': 'Change', 'body': '\\u0045' + 'sri'}}
        self.assertTrue(guard.find_pr_metadata_hits('fixture', payload))

    def test_source_escape_followed_by_html_entity_is_detected(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, '"' + '\\u0026' + '#69;' + 'sri"\n')
        self.assertTrue(guard.find_new_hits(root, 'fixture', base, head))

    def test_excessive_nested_encodings_fail_closed(self):
        with self.assertRaises(RuntimeError):
            guard.decode_rendered_text('&' + 'amp;' * 10 + '#69;' + 'sri')

    def test_many_long_escaped_terms_fail_before_classifier_stalls(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        line = 'a' * 900 + '\\u0041' + 'rcGIS\n'
        head = commit(root, line * 500)
        result = subprocess.run([sys.executable, guard.__file__, '--root', str(root), '--repo', 'fixture',
                                 '--base', base, '--head', head], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)

    def test_unreasonably_many_terms_in_one_line_fail_closed(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, ('Es' + 'ri ') * 513 + '\n')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_oversized_changed_blob_fails_closed(self):
        from unittest.mock import patch
        root = repo(self.tmp_path)
        base = commit(root, 'ok\n')
        head = commit(root, 'large content\n')
        with patch.object(guard, 'MAX_BLOB_BYTES', 10), self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_repeated_embedded_names_are_bounded(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, ('living' + 'atlas') * 513 + '\n')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_late_binary_marker_cannot_hide_utf16_term(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        data = b'\x89PNG\r\n\x1a\n' + b'A' * 9000 + b'\0' + ('Es' + 'ri').encode('utf-16-le')
        (root / 'map.png').write_bytes(data)
        git(root, 'add', '.')
        git(root, 'commit', '-qm', 'late binary marker')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, git(root, 'rev-parse', 'HEAD'))

    def test_repeated_names_in_long_identifier_are_deduplicated(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, ('a' * 2000 + 'arc' + 'gis') * 100 + '\n')
        with self.assertRaises(RuntimeError):
            guard.find_new_hits(root, 'fixture', base, head)

    def test_extremely_long_marked_identifier_fails_before_classifier_stalls(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, 'a' * 100000 + 'Arc' + 'GIS\n')
        result = subprocess.run([sys.executable, guard.__file__, '--root', str(root), '--repo', 'fixture',
                                 '--base', base, '--head', head], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)

    def test_many_long_marked_tokens_fail_before_classifier_stalls(self):
        root = repo(self.tmp_path)
        base = commit(root, 'baseline\n')
        head = commit(root, ' '.join(['a' * 4080 + 'Arc' + 'GIS'] * 30) + '\n')
        result = subprocess.run([sys.executable, guard.__file__, '--root', str(root), '--repo', 'fixture',
                                 '--base', base, '--head', head], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)

    def test_neutral_edit_does_not_inspect_unrelated_long_existing_data(self):
        root = repo(self.tmp_path)
        base = commit(root, 'data: ' + 'a' * 5000 + '\nx & y\n')
        head = commit(root, 'data: ' + 'a' * 5000 + '\nx & y\nneutral edit\n')
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, head), [])

    def test_deletion_only_with_existing_term_is_allowed(self):
        root = repo(self.tmp_path)
        base = commit(root, 'old ' + 'Es' + 'ri\nremove me\n')
        head = commit(root, 'old ' + 'Es' + 'ri\n')
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, head), [])

    def test_allowed_term_does_not_inspect_unrelated_long_existing_data(self):
        root = repo(self.tmp_path)
        base = commit(root, 'data: ' + 'a' * 5000 + '\n')
        head = commit(root, 'data: ' + 'a' * 5000 + '\n' + 'es' + 'ri' + 'GeometryPoint\n')
        self.assertEqual(guard.find_new_hits(root, 'fixture', base, head), [])
