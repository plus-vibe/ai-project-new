"""Offline safety and failure checks for the local experiment."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from run_extraction import NoRedirect, Ollama, extraction_text, prepare, run
from argparse import Namespace


class ExtractionChecks(unittest.TestCase):
    def test_remote_endpoints_rejected(self):
        for endpoint in ('http://example.com', 'https://127.0.0.1',
                         'http://127.0.0.1@evil.test', 'http://127.0.0.1/path',
                         'http://127.0.0.1?redirect=example.com'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                Ollama(endpoint, 1)

    def test_redirect_rejected(self):
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'http://example.com')

    def test_bad_generation_cannot_pass(self):
        for response in ({}, {'done': False, 'message': {'content': 'partial'}},
                         {'done': True, 'done_reason': 'length', 'message': {'content': 'cut'}},
                         {'done': True, 'done_reason': 'stop', 'message': {'content': ' '}}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                extraction_text(response)
        self.assertEqual(extraction_text({'done': True, 'done_reason': 'stop',
                                         'message': {'content': 'answer'}}), 'answer')

    def test_timeout_and_http_error_propagate(self):
        class BrokenOpener:
            def __init__(self, error):
                self.error = error

            def open(self, *args, **kwargs):
                raise self.error

        client = Ollama('http://127.0.0.1:11434', 1)
        for error in (TimeoutError(), HTTPError('http://127.0.0.1', 500, 'error', {}, None)):
            client.opener = BrokenOpener(error)
            with self.assertRaises(type(error)):
                client.request('chat', {})

    def test_orphan_skipped_and_ambiguous_source_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'input.zip'
            cases = root / 'cases.json'
            catalog = {'version': 'test', 'cases': [
                {'id': 'slide', 'source_image': 'slide.png', 'reviewed': False},
                {'id': 'orphan', 'source_image': 'orphan.png', 'reviewed': False}]}
            cases.write_text(json.dumps(catalog), encoding='utf-8')
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('note.md', '![[slide.png]]')
                z.writestr('slide.png', b'image')
                z.writestr('orphan.png', b'orphan')
            _, _, selected, skipped = prepare(archive, cases)
            self.assertEqual([case['id'] for case, _ in selected], ['slide'])
            self.assertEqual(skipped, [{'case_id': 'orphan', 'reason': 'unused_attachment'}])
            with zipfile.ZipFile(archive, 'a') as z:
                z.writestr('other/slide.png', b'other')
            with self.assertRaises(ValueError):
                prepare(archive, cases)

    def test_timeout_leaves_failed_manifest_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive, prompt = root / 'input.zip', root / 'prompt.md'
            archive.write_bytes(b'test archive')
            prompt.write_text('Extract image', encoding='utf-8')
            args = Namespace(archive=archive, cases=root / 'cases.json', prompt=prompt,
                             endpoint='http://127.0.0.1:11434', timeout=1,
                             num_predict=10, num_ctx=1024, model='test', save_responses=True)
            case = {'id': 'test', 'source_image': 'test.png', 'reviewed': False}

            def reply(route, payload=None):
                if route == 'version':
                    return {'version': 'test'}
                if route == 'tags':
                    return {'models': [{'name': 'test', 'digest': 'test-digest'}]}
                if route == 'ps':
                    return {'models': []}
                raise TimeoutError()

            with patch('run_extraction.ROOT', root), patch('run_extraction.Ollama') as client, \
                    patch('run_extraction.prepare', return_value=(
                        {'version': 'test'}, b'{}', [(case, b'image')], [])):
                client.return_value.request.side_effect = reply
                client.return_value.endpoint = args.endpoint
                self.assertEqual(run(args), 1)
            manifest = json.loads(next((root / 'artifacts/runs').glob('*/manifest.json'))
                                  .read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'failed')
            self.assertEqual(manifest['results'][0]['error_type'], 'TimeoutError')
            self.assertNotIn('response_file', manifest['results'][0])


if __name__ == '__main__':
    unittest.main()
