"""Local web app: bounded ZIP input, ordered summaries, and Markdown export."""
import io
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time
import unicodedata
from urllib.parse import unquote
import uuid
import zipfile

from inspect_zip import inspect
from run_extraction import Ollama
from lecture_notes_ai.pipeline import VERSION, analyze

ROOT = Path(__file__).resolve().parents[2]
MAX_UPLOAD = 30 * 1024**2
EMBED = re.compile(r'!\[\[([^\]\n]+)\]\]|!\[[^\]\n]*\]\(<([^>\n]+)>\)|!\[[^\]\n]*\]\(([^)\n]+)\)')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_lecture(path):
    # Reuse the existing bounded ZIP validator; no archive member is extracted.
    inventory = inspect(path)
    with zipfile.ZipFile(path) as archive:
        note = inventory['note']
        text = archive.read(note).decode('utf-8-sig')
        images = [n for n in archive.namelist()
                  if PurePosixPath(n).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}]
        blocks, warnings, used = [], [], set()

        def new_block(line, target=None):
            block = {'id': f'b{len(blocks) + 1:03d}', 'line_start': line, 'line_end': line,
                     'text': '', 'image': None, 'note': note}
            if target:
                target = unicodedata.normalize('NFC', unquote(target.split('|', 1)[0].split('#', 1)[0].strip()))
                block['attachment_target'] = target
                candidate = PurePosixPath(target)
                if (candidate.is_absolute() or '..' in candidate.parts or '\\' in target
                        or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target)):
                    warnings.append(f'Строка {line}: внешнее или небезопасное вложение пропущено.')
                else:
                    local = str(PurePosixPath(note).parent / candidate)
                    matches = [n for n in images if unicodedata.normalize('NFC', n) == unicodedata.normalize('NFC', local)]
                    if not matches:
                        matches = [n for n in images if unicodedata.normalize('NFC', n) == target]
                    if not matches:
                        matches = [n for n in images if unicodedata.normalize('NFC', PurePosixPath(n).name) == candidate.name]
                    if len(matches) == 1:
                        block['image'] = matches[0]
                        used.add(matches[0])
                    else:
                        warnings.append(f'Строка {line}: вложение не найдено или имя неоднозначно.')
                block['attachment_requested'] = True
            blocks.append(block)
            return block

        current, fence = None, None
        for number, line in enumerate(text.splitlines(), 1):
            marker = re.match(r'^\s*(`{3,}|~{3,})', line)
            if marker:
                if fence is None:
                    fence = marker[1][0]
                elif marker[1][0] == fence:
                    fence = None
                matches = []
            else:
                matches = [] if fence else list(EMBED.finditer(line))
            if matches:
                end = 0
                for match in matches:
                    prefix = line[end:match.start()].strip()
                    if prefix:
                        current = current or new_block(number)
                        current['text'] += prefix + '\n'
                    current = new_block(number, next(g for g in match.groups() if g is not None))
                    end = match.end()
                line = line[end:]
            elif re.match(r'^#{1,6}\s', line) and fence is None:
                current = new_block(number)
            if line.strip() or current is not None:
                current = current or new_block(number)
                current['text'] += line + '\n'
                current['line_end'] = number
        blocks = [b for b in blocks if b['image'] or b['text'].strip() or b.get('attachment_requested')]
        if not blocks:
            raise ValueError('В заметке нет текста или поддерживаемых вложений.')
        if len(blocks) > 100:
            raise ValueError('Для MVP поддерживается до 100 блоков в одной лекции.')
        for block in blocks:
            block['text'] = block['text'].strip()
            if len(block['text']) > 16000:
                raise ValueError('Слишком большой текстовый блок: разделите заметку заголовками.')
        unused = len(set(images) - used)
        if unused:
            warnings.append(f'Неиспользуемых изображений: {unused}. Они исключены из обработки.')
        missing = [b['attachment_target'] for b in blocks
                   if b.get('attachment_requested') and not b['image']]
        return {'title': PurePosixPath(note).stem, 'note': note, 'blocks': blocks,
                'image_count': len(used), 'total_images': len(images), 'warnings': warnings,
                'missing_images': missing,
                'requested_images': sum(bool(b.get('attachment_requested')) for b in blocks)}


def export_markdown(job):
    lines = [f"# {job['title']}", '', f"Модель: {job['model']}. Конспект создан ИИ; сверьте важные детали с источником.", '']
    if job.get('overview'):
        lines += [job['overview'], '']
    for section in job['sections']:
        ids = section.get('source_ids', [section.get('source_id')])
        lines += [f"## {section['title']}", '', section['summary'], '',
                  'Источники: ' + ', '.join(f'[{s}](#source-{s})' for s in ids), '']
    if job['warnings']:
        lines += ['## На что обратить внимание', '']
        lines += ['- ' + warning.replace('\n', ' ') for warning in job['warnings']]
        lines.append('')
    lines += ['## Источники', '', 'Пути ниже относятся к исходному ZIP. Изображения в этот Markdown не включены.', '']
    for block in job['blocks']:
        lines += [f'<a id="source-{block["id"]}"></a>',
                  f"- **{block['id']}** — {block['note']}, строки {block['line_start']}–{block['line_end']}."]
        if block['image']:
            lines += [f"  Изображение: {block['image']}"]
    return '\n'.join(lines) + '\n'


class Store:
    def __init__(self, root, endpoint='http://127.0.0.1:11434', client_factory=Ollama):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.endpoint = endpoint
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.gpu = threading.Lock()
        self.jobs = {}
        for file in self.root.glob('*/job.json'):
            try:
                job = json.loads(file.read_text(encoding='utf-8'))
                if job['id'] != file.parent.name or not re.fullmatch(r'[a-f0-9]{32}', job['id']):
                    continue
                if job['status'] in ('running', 'queued'):
                    job.update(status='error', error='Обработка прервана. Можно повторить запуск.')
                if 'missing_images' not in job:
                    lecture = parse_lecture(file.parent / 'input.zip')
                    for key in ('blocks', 'missing_images', 'requested_images'):
                        job[key] = lecture[key]
                self.jobs[job['id']] = job
            except (OSError, ValueError, KeyError):
                continue

    def save(self, job):
        target = self.root / job['id'] / 'job.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(target)

    def snapshot(self, job_id):
        with self.lock:
            return json.loads(json.dumps(self.jobs[job_id]))

    def list_jobs(self):
        with self.lock:
            return [{k: job.get(k) for k in ('id', 'title', 'status', 'created_at', 'image_count')}
                    for job in sorted(self.jobs.values(), key=lambda j: j['created_at'], reverse=True)]

    def upload(self, data):
        if not data or len(data) > MAX_UPLOAD:
            raise ValueError('Выберите ZIP размером до 30 МБ.')
        job_id = uuid.uuid4().hex
        folder = self.root / job_id
        folder.mkdir()
        path = folder / 'input.zip'
        path.write_bytes(data)
        try:
            lecture = parse_lecture(path)
        except Exception:
            # Remove only files just created by this request, never a directory tree.
            path.unlink()
            folder.rmdir()
            raise
        job = dict(lecture, id=job_id, created_at=datetime.now(timezone.utc).isoformat(),
                   status='ready', progress=0, total=len(lecture['blocks']), sections=[],
                   base_warnings=list(lecture['warnings']), model=None, error=None,
                   archive_sha256=digest(data), events=[], duration_seconds=None)
        with self.lock:
            self.jobs[job_id] = job
            self.save(job)
        return self.snapshot(job_id)

    def attachments(self, job_id, data):
        """Create a repaired copy, keeping the original lecture upload intact."""
        if len(data) > MAX_UPLOAD:
            raise ValueError('Изображения должны занимать не больше 30 МБ.')
        original = self.root / self.snapshot(job_id)['id'] / 'input.zip'
        stream = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as added:
            items = added.infolist()
            if len(items) > 1000 or sum(i.file_size for i in items) > MAX_UPLOAD:
                raise ValueError('Слишком много изображений или слишком большой размер.')
            if len({i.filename for i in items}) != len(items):
                raise ValueError('В архиве добавляемых изображений повторяются пути.')
            for item in items:
                name, path = item.filename, PurePosixPath(item.filename)
                if (path.is_absolute() or '..' in path.parts or '\\' in name
                        or re.match(r'^[A-Za-z]:', name) or stat.S_ISLNK(item.external_attr >> 16)
                        or item.file_size > MAX_UPLOAD):
                    raise ValueError('Недопустимый путь изображения.')
                if not item.is_dir() and path.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
                    raise ValueError('Добавляйте только PNG, JPEG или WebP.')
            replacements = {i.filename: added.read(i) for i in items if not i.is_dir()}
            with zipfile.ZipFile(original) as old, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as combined:
                for item in old.infolist():
                    if not item.is_dir() and item.filename not in replacements:
                        combined.writestr(item.filename, old.read(item))
                for name, content in replacements.items():
                    combined.writestr(name, content)
        return self.upload(stream.getvalue())

    def models(self):
        client = self.client_factory(self.endpoint, 5)
        version = client.request('version')['version']
        models = client.request('tags').get('models', [])
        return {'online': True, 'version': version, 'models': [m['name'] for m in models
                if 'vision' in m.get('capabilities', []) or
                'clip' in m.get('details', {}).get('families', []) or
                'vl' in m.get('details', {}).get('family', '')]}

    def start(self, job_id, model, detail='brief'):
        if detail not in ('brief', 'full'):
            raise ValueError('Неизвестная подробность конспекта.')
        with self.lock:
            job = self.jobs[job_id]
            if job['status'] in ('queued', 'running'):
                raise ValueError('Эта лекция уже обрабатывается.')
            lecture = parse_lecture(self.root / job_id / 'input.zip')
            if lecture['missing_images']:
                raise ValueError(f"Не хватает {len(lecture['missing_images'])} скриншотов. Добавьте файлы изображений перед обработкой.")
            job.update(lecture)
            job.update(status='queued', model=model, detail=detail, error=None,
                       progress=0, sections=[], overview='', extractions=[], images_read=0,
                       duration_seconds=None,
                       phase='reading', pipeline_version=VERSION,
                       warnings=list(lecture['warnings']), base_warnings=list(lecture['warnings']), events=[])
            self.save(job)
        thread = threading.Thread(target=self.process, args=(job_id,), daemon=True)
        thread.start()

    def process(self, job_id):
        with self.gpu:
            started = time.perf_counter()
            with self.lock:
                job = self.jobs[job_id]
                job.update(status='running', stage='Подключаем локальную модель')
                self.save(job)
            try:
                client = self.client_factory(self.endpoint, 300)
                tags = client.request('tags')['models']
                installed = next((m for m in tags if m['name'] == job['model']), None)
                if installed is None:
                    raise ValueError('Выбранная модель не установлена в Ollama. Ничего не скачивалось.')
                with self.lock:
                    job.update(model_digest=installed['digest'],
                               ollama_version=client.request('version')['version'])

                def update(**changes):
                    with self.lock:
                        extraction = changes.pop('extraction', None)
                        event = changes.pop('event', None)
                        if extraction:
                            job['extractions'].append(extraction)
                        if event:
                            job['events'].append(event)
                        job.update(changes)
                        self.save(job)

                with zipfile.ZipFile(self.root / job_id / 'input.zip') as archive:
                    summary, warnings = analyze(client, job['model'], installed['digest'],
                        job['blocks'], archive, self.root / 'cache', job['detail'], update)
                with self.lock:
                    job.update(status='done', phase='done', stage='Summary лекции готов',
                               overview=summary['overview'], sections=summary['sections'],
                               warnings=job['warnings'] + warnings,
                               duration_seconds=round(time.perf_counter() - started, 1))
                    (self.root / job_id / 'summary.md').write_bytes(export_markdown(job).encode('utf-8'))
                    self.save(job)
            except Exception as exc:
                with self.lock:
                    message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else (
                        'Не удалось завершить обработку. Проверьте Ollama и повторите запуск; прочитанные скриншоты сохранены в кэше.')
                    job.update(status='error', error=message, error_type=type(exc).__name__)
                    self.save(job)

    def image(self, job_id, source_id):
        job = self.snapshot(job_id)
        block = next((b for b in job['blocks'] if b['id'] == source_id), None)
        if not block or not block['image']:
            raise KeyError('image')
        with zipfile.ZipFile(self.root / job_id / 'input.zip') as archive:
            return archive.read(block['image']), mimetypes.guess_type(block['image'])[0] or 'application/octet-stream'
