"""MVP pipeline and HTTP checks using synthetic inputs and a fake model."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'src')]
from lecture_notes_ai.app import Store, export_markdown, parse_lecture
from lecture_notes_ai.pipeline import valid_summary
from serve import handler_for


def archive_bytes(note='![[a.png|400]]\nКомментарий один\n# Раздел\nТекст\n![слайд](b.png)\nКомментарий два', extra=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('lesson/note.md', note)
        archive.writestr('lesson/a.png', b'png a')
        archive.writestr('lesson/b.png', b'png b')
        archive.writestr('orphan.png', b'not used')
        for key, value in (extra or {}).items():
            archive.writestr(key, value)
    return data.getvalue()


class FakeModel:
    calls = []

    def __init__(self, endpoint, timeout):
        pass

    def request(self, route, payload=None):
        if route == 'tags':
            return {'models': [{'name': 'local:vision', 'digest': 'test-digest', 'capabilities': ['vision']}]}
        if route == 'version':
            return {'version': 'test'}
        if route == 'chat':
            self.calls.append(payload)
            if 'content' in payload['format']['properties']:
                return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(
                    {'title': 'Прочитанный слайд', 'content': 'Важный факт только на скриншоте: 42.', 'warnings': []})}}
            material = json.loads(payload['messages'][1]['content'])
            if 'sections' in material:
                return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(
                    {'overview': 'Общий смысл лекции.'})}}
            return {'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(
                {'title': 'Общая тема',
                  'summary': 'Важный факт со скриншота: 42. Комментарии поясняют его.',
                  'warnings': []})}}
        raise AssertionError(route)


class AppChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, client_factory=FakeModel)
        FakeModel.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def finish(self, job_id):
        for _ in range(200):
            job = self.store.snapshot(job_id)
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.01)
        self.fail('Worker did not finish')

    def test_sources_order_context_and_code_fences(self):
        job = self.store.upload(archive_bytes())
        blocks = job['blocks']
        self.assertEqual(len(blocks), 3)
        self.assertTrue(blocks[0]['image'].endswith('a.png'))
        self.assertEqual(blocks[0]['text'], 'Комментарий один')
        self.assertIsNone(blocks[1]['image'])
        self.assertTrue(blocks[1]['text'].startswith('# Раздел'))
        self.assertTrue(blocks[2]['image'].endswith('b.png'))
        self.assertEqual(blocks[2]['text'], 'Комментарий два')
        self.assertEqual(job['image_count'], 2)
        other = self.store.upload(archive_bytes('```md\n![[missing.png]]\n```\n![img](<a.png>)'))
        self.assertEqual(other['image_count'], 1)
        self.assertIn('![[missing.png]]', other['blocks'][0]['text'])
        flat_selection = self.store.upload(archive_bytes('![[attachments/a.png]]'))
        self.assertEqual(flat_selection['missing_images'], [])
        self.assertTrue(flat_selection['blocks'][0]['image'].endswith('/a.png'))

    def test_bounded_zip_no_extraction_and_warning_for_remote(self):
        for extra in ({'../escape.txt': 'bad'}, {'/absolute.txt': 'bad'}):
            with self.assertRaises(ValueError):
                self.store.upload(archive_bytes(extra=extra))
        self.assertEqual(list(self.root.iterdir()), [])
        job = self.store.upload(archive_bytes('![remote](https://example.test/pic.png)\n![[missing.png]]'))
        self.assertEqual(job['image_count'], 0)
        self.assertGreaterEqual(len(job['warnings']), 2)
        self.assertTrue(all(b['image'] is None for b in job['blocks']))

    def test_pipeline_export_restart_and_cache(self):
        job = self.store.upload(archive_bytes())
        self.store.start(job['id'], 'local:vision')
        done = self.finish(job['id'])
        self.assertEqual(done['status'], 'done')
        self.assertEqual(done['sections'][0]['source_ids'], [b['id'] for b in done['blocks']])
        self.assertEqual(len(done['sections']), 1)
        self.assertEqual(len(done['extractions']), 2)
        self.assertEqual(len(FakeModel.calls), 4)
        self.assertNotIn('Комментарий один', FakeModel.calls[0]['messages'][1]['content'])
        self.assertIn('images', FakeModel.calls[0]['messages'][1])
        self.assertIn('images', FakeModel.calls[1]['messages'][1])
        final = FakeModel.calls[2]['messages'][1]
        self.assertNotIn('images', final)
        self.assertIn('Комментарий один', final['content'])
        self.assertIn('Важный факт только на скриншоте: 42.', final['content'])
        output = export_markdown(done)
        self.assertIn('](#source-b001)', output)
        self.assertIn('id="source-b001"', output)
        restored = Store(self.root, client_factory=FakeModel)
        self.assertEqual(restored.snapshot(job['id'])['status'], 'done')
        self.store.start(job['id'], 'local:vision')
        again = self.finish(job['id'])
        self.assertEqual(again['status'], 'done')
        self.assertEqual(len(FakeModel.calls), 4)
        self.assertTrue(all(e['cached'] for e in again['events']))

    def test_missing_screenshots_block_start_and_can_be_added(self):
        original = self.store.upload(archive_bytes('![[missing.png]]\nПодпись недостаточна'))
        self.assertEqual(original['missing_images'], ['missing.png'])
        with self.assertRaisesRegex(ValueError, 'Не хватает 1'):
            self.store.start(original['id'], 'local:vision')
        self.assertFalse(FakeModel.calls)
        extra = io.BytesIO()
        with zipfile.ZipFile(extra, 'w') as z:
            z.writestr('missing.png', b'new screenshot')
        repaired = self.store.attachments(original['id'], extra.getvalue())
        self.assertNotEqual(repaired['id'], original['id'])
        self.assertEqual(repaired['missing_images'], [])
        self.assertEqual(self.store.snapshot(original['id'])['missing_images'], ['missing.png'])
        self.store.start(repaired['id'], 'local:vision')
        self.assertEqual(self.finish(repaired['id'])['status'], 'done')

    def test_summary_cannot_silently_drop_or_reorder_sources(self):
        value = {'overview': 'Обзор', 'sections': [{'title': 'Тема', 'summary': 'Текст',
                 'source_ids': ['b002', 'b001']}], 'warnings': []}
        with self.assertRaises(ValueError):
            valid_summary(value, ['b001', 'b002'])
        value['sections'][0]['source_ids'] = ['b001']
        with self.assertRaises(ValueError):
            valid_summary(value, ['b001', 'b002'])

    def test_plain_text_lecture_still_gets_whole_summary(self):
        job = self.store.upload(archive_bytes('# Тема\nТолько текст лекции.'))
        self.store.start(job['id'], 'local:vision')
        done = self.finish(job['id'])
        self.assertEqual(done['status'], 'done')
        self.assertEqual(done['images_read'], 0)
        self.assertEqual(done['extractions'], [])
        self.assertEqual(len(done['sections']), 1)
        self.assertTrue(all('images' not in call['messages'][1] for call in FakeModel.calls))

    def test_model_failure_is_error_and_can_retry(self):
        job = self.store.upload(archive_bytes())
        class Broken(FakeModel):
            def request(self, route, payload=None):
                if route == 'chat':
                    raise TimeoutError()
                return super().request(route, payload)
        self.store.client_factory = Broken
        self.store.start(job['id'], 'local:vision')
        failed = self.finish(job['id'])
        self.assertEqual(failed['status'], 'error')
        self.assertFalse((self.root / job['id'] / 'summary.md').exists())
        self.store.client_factory = FakeModel
        self.store.start(job['id'], 'local:vision')
        self.assertEqual(self.finish(job['id'])['status'], 'done')

    def test_http_upload_start_download_and_origin_guard(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base + '/') as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'/app.js', response.read())
            request = Request(base + '/api/jobs', data=archive_bytes(), headers={'Content-Type': 'application/zip'})
            with urlopen(request) as response:
                job = json.load(response)
            path = base + '/api/jobs/' + job['id']
            with self.assertRaises(HTTPError) as caught:
                urlopen(path + '/download')
            self.assertEqual(caught.exception.code, 409)
            with urlopen(Request(path + '/start', data=b'{"model":"local:vision"}',
                                 headers={'Content-Type': 'application/json'})) as response:
                self.assertEqual(response.status, 202)
            self.assertEqual(self.finish(job['id'])['status'], 'done')
            with urlopen(path + '/download') as response:
                self.assertIn('attachment', response.headers['Content-Disposition'])
                self.assertIn('## Источники', response.read().decode())
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base + '/api/jobs', data=archive_bytes(), headers={'Origin': 'https://evil.test'}))
            self.assertEqual(caught.exception.code, 403)
            incomplete = self.store.upload(archive_bytes('![[missing.png]]'))
            self.store.jobs[incomplete['id']]['status'] = 'done'  # Simulate a legacy incomplete result.
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + '/api/jobs/' + incomplete['id'] + '/download')
            self.assertEqual(caught.exception.code, 409)
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(base + '/', headers={'Host': 'evil.test'}))
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
