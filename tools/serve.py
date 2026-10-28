"""Run the local lecture notes MVP with no third-party dependencies."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from lecture_notes_ai.app import MAX_UPLOAD, Store, export_markdown
from lecture_notes_ai.pipeline import VERSION

WEB = ROOT / 'web'


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Do not log uploaded filenames or lecture content.

        def reply(self, status, data, content_type='application/json; charset=utf-8', extra=None):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def local_request(self):
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in allowed:
                return False
            origin = self.headers.get('Origin')
            return not origin or origin in {'http://' + host for host in allowed}

        def do_GET(self):
            if not self.local_request():
                return self.reply(403, {'error': 'Доступ разрешён только с локальной страницы.'})
            route = urlsplit(self.path).path
            try:
                static = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/attachments.js': ('attachments.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if route in static:
                    name, kind = static[route]
                    return self.reply(200, (WEB / name).read_bytes(), kind)
                if route == '/api/models':
                    try:
                        return self.reply(200, store.models())
                    except Exception:
                        return self.reply(200, {'online': False, 'models': [],
                                               'error': 'Ollama не отвечает. Запустите её и обновите статус.'})
                if route == '/api/jobs':
                    return self.reply(200, store.list_jobs())
                match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(download|images/b\d+))?', route)
                if match:
                    job_id, action = match.groups()
                    job = store.snapshot(job_id)
                    if action == 'download':
                        if job.get('missing_images'):
                            return self.reply(409, {'error': 'В лекции отсутствуют скриншоты. Сначала добавьте файлы и пересоберите summary.'})
                        if job['status'] != 'done' or job.get('pipeline_version') != VERSION:
                            return self.reply(409, {'error': 'Общий summary ещё не готов. Запустите обработку.'})
                        return self.reply(200, export_markdown(job).encode('utf-8'), 'text/markdown; charset=utf-8',
                                          {'Content-Disposition': 'attachment; filename="lecture-notes.md"'})
                    if action:
                        data, kind = store.image(job_id, action.split('/')[1])
                        return self.reply(200, data, kind)
                    return self.reply(200, job)
                return self.reply(404, {'error': 'Страница не найдена.'})
            except KeyError:
                return self.reply(404, {'error': 'Лекция или источник не найдены.'})
            except (OSError, ValueError, zipfile.BadZipFile):
                return self.reply(500, {'error': 'Не удалось прочитать локальные данные.'})

        def do_POST(self):
            if not self.local_request():
                return self.reply(403, {'error': 'Недопустимый источник запроса.'})
            route = urlsplit(self.path).path
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > MAX_UPLOAD:
                    return self.reply(413, {'error': 'Размер запроса должен быть от 1 байта до 30 МБ.'})
                self.connection.settimeout(30)
                data = self.rfile.read(size)
                if len(data) != size:
                    return self.reply(400, {'error': 'Загрузка прервана. Выберите файл ещё раз.'})
                if route == '/api/jobs':
                    return self.reply(201, store.upload(data))
                attachment = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/attachments', route)
                if attachment:
                    return self.reply(201, store.attachments(attachment[1], data))
                match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})/start', route)
                if match:
                    args = json.loads(data)
                    if not isinstance(args, dict) or not isinstance(args.get('model'), str) or not 1 <= len(args['model']) <= 200:
                        raise ValueError('Выберите установленную модель.')
                    store.start(match[1], args['model'], args.get('detail', 'brief'))
                    return self.reply(202, store.snapshot(match[1]))
                return self.reply(404, {'error': 'Неизвестная команда.'})
            except KeyError:
                return self.reply(404, {'error': 'Лекция не найдена.'})
            except (ValueError, zipfile.BadZipFile, UnicodeError, RuntimeError) as exc:
                message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Не удалось прочитать ZIP. Нужна одна UTF-8 Markdown-заметка с изображениями.'
                return self.reply(400, {'error': message})
            except OSError:
                return self.reply(500, {'error': 'Ошибка чтения или записи локального файла.'})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--endpoint', default='http://127.0.0.1:11434')
    parser.add_argument('--open', action='store_true', help='Open the local app in the default browser')
    args = parser.parse_args()
    store = Store(ROOT / 'artifacts/app', args.endpoint)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(store))
    server.daemon_threads = True
    print(f'Lecture Notes: http://127.0.0.1:{server.server_port}', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{server.server_port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
