"""Explicit local Ollama experiment, not a summarizer or quality evaluator."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import platform
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

from inspect_zip import inspect

ROOT = Path(__file__).resolve().parents[1]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Server redirects are not allowed')


class Ollama:
    def __init__(self, endpoint, timeout):
        url = urlsplit(endpoint)
        try:
            local = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            local = False
        if (url.scheme != 'http' or not local or url.username or url.password
                or url.path not in ('', '/') or url.query or url.fragment):
            raise ValueError('Endpoint must be an HTTP loopback IP, e.g. http://127.0.0.1:11434')
        self.endpoint = endpoint.rstrip('/')
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, route, payload=None):
        body = None if payload is None else json.dumps(payload).encode('utf-8')
        req = Request(self.endpoint + '/api/' + route, data=body,
                      headers={'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=self.timeout) as response:
            raw = response.read(8 * 1024**2 + 1)
        if len(raw) > 8 * 1024**2:
            raise ValueError('Server response exceeds 8 MiB')
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('error'):
            raise ValueError('Server returned an invalid response or error')
        return result


def prepare(archive_path, catalog_path):
    inventory = inspect(archive_path)
    if inventory['missing'] or inventory['ambiguous']:
        raise ValueError('Resolve missing or ambiguous wiki embeds before the experiment')
    raw_catalog = catalog_path.read_bytes()
    catalog = json.loads(raw_catalog)
    cases = catalog['cases']
    ids = [case['id'] for case in cases]
    if not cases or len(set(ids)) != len(ids):
        raise ValueError('Expected nonempty cases with unique IDs')
    selected, skipped = [], []
    with zipfile.ZipFile(archive_path) as archive:
        images = [entry for entry in archive.infolist()
                  if not entry.is_dir() and PurePosixPath(entry.filename).suffix.lower()
                  in {'.png', '.jpg', '.jpeg', '.webp'}]
        for case in cases:
            matches = [entry for entry in images
                       if PurePosixPath(entry.filename).name == case['source_image']]
            if len(matches) != 1:
                raise ValueError(f"Expected one source image for case {case['id']}")
            entry = matches[0]
            if entry.filename in inventory['unused_images']:
                skipped.append({'case_id': case['id'], 'reason': 'unused_attachment'})
                continue
            selected.append((case, archive.read(entry)))
    if not selected:
        raise ValueError('No referenced images selected')
    return catalog, raw_catalog, selected, skipped


def extraction_text(response):
    message = response.get('message')
    if not isinstance(message, dict):
        raise ValueError('Missing assistant message')
    text = message.get('content')
    if (response.get('done') is not True or not isinstance(text, str) or not text.strip()
            or message.get('tool_calls') or response.get('done_reason') != 'stop'):
        raise ValueError('Empty, truncated or unexpected model response')
    return text


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run(args):
    if args.timeout <= 0 or args.num_predict <= 0 or args.num_ctx <= 0:
        raise ValueError('Timeout and token limits must be positive')
    client = Ollama(args.endpoint, args.timeout)
    archive_path = args.archive.resolve()
    catalog, raw_catalog, selected, skipped = prepare(archive_path, args.cases)
    prompt_bytes = args.prompt.read_bytes()
    prompt = prompt_bytes.decode('utf-8-sig')
    version = client.request('version')['version']
    models = client.request('tags').get('models', [])
    model = next((model for model in models if model['name'] == args.model), None)
    if model is None:
        raise ValueError('Requested model is not installed; nothing will be downloaded')
    loaded_before = client.request('ps')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT / 'artifacts' / 'runs' / run_id
    output.mkdir(parents=True, exist_ok=False)
    options = {'temperature': 0, 'seed': 42, 'num_ctx': args.num_ctx,
               'num_predict': args.num_predict}
    git = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                         capture_output=True, text=True, check=False)
    dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT,
                           capture_output=True, text=True, check=False)
    manifest = {
        'run_id': run_id, 'status': 'running', 'mode': 'image_only',
        'model': args.model, 'model_digest': model['digest'],
        'model_details': model.get('details'), 'ollama_version': version,
        'endpoint': client.endpoint, 'python': platform.python_version(),
        'platform': platform.platform(), 'git_commit': git.stdout.strip(),
        'git_dirty': bool(dirty.stdout.strip()),
        'script_sha256': sha256(Path(__file__).read_bytes()),
        'archive_sha256': sha256(archive_path.read_bytes()),
        'catalog_version': catalog['version'], 'catalog_sha256': sha256(raw_catalog),
        'prompt_sha256': sha256(prompt_bytes), 'options': options,
        'timeout_seconds': args.timeout, 'repeats': 1, 'retries': 0,
        'initial_loaded_models': loaded_before.get('models', []),
        'timing_mode': 'initial server state recorded; no forced cold/warm control',
        'vram_peak': None, 'quality_evaluation': 'not_performed',
        'responses_saved': args.save_responses, 'skipped': skipped, 'results': [],
    }
    # Freeze the inputs needed to reproduce the run, without copying lecture bytes.
    (output / 'prompt.md').write_bytes(prompt_bytes)
    (output / 'cases.json').write_bytes(raw_catalog)
    write_json(output / 'manifest.json', manifest)
    print(f'Run directory: {output}', flush=True)
    failed = False
    try:
        for index, (case, image) in enumerate(selected, 1):
            record = {'case_id': case['id'], 'source_image': case['source_image'],
                      'image_sha256': sha256(image), 'reviewed': case['reviewed'],
                      'status': 'running'}
            manifest['results'].append(record)
            write_json(output / 'manifest.json', manifest)
            print(f"[{index}/{len(selected)}] {case['id']} ...", flush=True)
            start = time.perf_counter()
            try:
                response = client.request('chat', {
                    'model': args.model, 'stream': False, 'think': False,
                    'keep_alive': '5m', 'options': options,
                    'messages': [{'role': 'user', 'content': prompt,
                                  'images': [base64.b64encode(image).decode('ascii')]}],
                })
                # Keep partial content for diagnosis only with explicit opt-in.
                content = response.get('message', {}).get('content')
                if args.save_responses and isinstance(content, str):
                    name = f'{index:02d}-response.txt'
                    (output / name).write_text(content, encoding='utf-8')
                    record['response_file'] = name
                extraction_text(response)
                record['status'] = 'ok'
                for key in ('done_reason', 'total_duration', 'load_duration',
                            'prompt_eval_count', 'prompt_eval_duration',
                            'eval_count', 'eval_duration'):
                    record[key] = response.get(key)
            except Exception as exc:
                record['status'] = 'error'
                record['error_type'] = type(exc).__name__
                failed = True
            finally:
                record['elapsed_seconds'] = round(time.perf_counter() - start, 3)
                write_json(output / 'manifest.json', manifest)
            print(f"  {record['status']}, {record['elapsed_seconds']} s", flush=True)
    finally:
        manifest['status'] = ('incomplete' if any(r['status'] == 'running'
                              for r in manifest['results']) else
                              'failed' if failed else 'completed')
        manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(output / 'manifest.json', manifest)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--cases', type=Path, default=ROOT / 'evals/golden/cases.json')
    parser.add_argument('--prompt', type=Path, default=ROOT / 'prompts/extract-slide.v1.md')
    parser.add_argument('--model', default='qwen3-vl:4b-instruct')
    parser.add_argument('--endpoint', default='http://127.0.0.1:11434')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--num-ctx', type=int, default=8192)
    parser.add_argument('--num-predict', type=int, default=4096)
    parser.add_argument('--save-responses', action='store_true',
                        help='Opt in to saving lecture-derived responses locally outside Git')
    args = parser.parse_args()
    try:
        return run(args)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        # Server errors may contain source data; do not print raw HTTP bodies.
        parser.exit(1, f'Experiment failed: {type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
