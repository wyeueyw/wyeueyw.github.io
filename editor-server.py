#!/usr/bin/env python3
"""Local-only website editor: preserve, commit, then push index.html."""
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
from http.client import HTTPConnection
import argparse
import datetime
import errno
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import threading
import webbrowser

ROOT = Path(__file__).resolve().parent
EXPECTED_REMOTES = {'https://github.com/wyeueyw/wyeueyw.github.io.git', 'git@github.com:wyeueyw/wyeueyw.github.io.git'}
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
SITE = 'https://wyeueyw.github.io/'


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def git(*args, timeout=60):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    # Use the system Git without depending on a shell's PATH.
    result = subprocess.run(['/usr/bin/git', '-C', str(ROOT), *args], capture_output=True, text=True, env=env, timeout=timeout, close_fds=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or 'Git command failed.')
    return result.stdout.strip()


def verify_repo():
    if Path(git('rev-parse', '--show-toplevel')).resolve() != ROOT.resolve():
        raise RuntimeError('MY PAGE 自体をGitリポジトリにしてください。')
    if git('branch', '--show-current') != 'main':
        raise RuntimeError('公開ブランチ main に切り替えてください。')
    if git('remote', 'get-url', '--push', 'origin') not in EXPECTED_REMOTES:
        raise RuntimeError('origin が指定の公開リポジトリと一致しません。')


def write_atomic(path, content):
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    try:
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def push_changes():
    verify_repo()
    # A normal push rejects remote changes; never force-push or reset the user's work.
    git('push', 'origin', 'main', timeout=90)
    return git('rev-parse', '--short', 'HEAD')


def save_and_publish(payload):
    verify_repo()
    path = ROOT / 'index.html'
    before = path.read_text(encoding='utf-8')
    if payload.get('baseHash') != digest(before):
        raise ValueError('別の操作で index.html が変更されています。最新版を開き直してください。')
    content = payload.get('html')
    if not isinstance(content, str) or len(content.encode('utf-8')) > 3_000_000:
        raise ValueError('HTMLのサイズが不正です。')
    if '<!doctype html>' not in content.lower() or 'item-dateline' not in content or '</html>' not in content.lower():
        raise ValueError('保存するHTMLの形式を確認してください。')
    # Do not include unrelated pending commits or staged files in an automatic publication.
    ahead = git('rev-list', 'origin/main..HEAD')
    if ahead:
        raise ValueError('まだ送信されていないコミットがあります。「pushを再試行」で送信してから保存してください。')
    if content != before:
        backup = ROOT / '.editor-backups'
        backup.mkdir(exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        (backup / ('index-' + stamp + '.html')).write_text(before, encoding='utf-8')
        write_atomic(path, content)
    result = {'saved': True, 'committed': False, 'baseHash': digest(content), 'site': SITE}
    try:
        # --only confines the commit to the requested page, even with other staged files.
        if git('status', '--porcelain', '--', 'index.html'):
            git('add', '--', 'index.html')
            git('commit', '--only', '-m', 'Update website content', '--', 'index.html')
        result['committed'] = True
        result['commit'] = push_changes()
        result['pushed'] = True
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        result.update(pushed=False, error=str(error))
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Never log the per-launch token or submitted content.
        pass

    def valid_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def send(self, status, body, content_type='application/json; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Invalid host.'})
        path = urlsplit(self.path).path
        if path in ('/', '/editor.html'):
            page = (ROOT / 'editor.html').read_text(encoding='utf-8')
            page = page.replace('<head>', '<head>\n    <meta name="local-editor-token" content="' + TOKEN + '">', 1)
            return self.send(200, page, 'text/html; charset=utf-8')
        if path == '/api/page':
            content = (ROOT / 'index.html').read_text(encoding='utf-8')
            try:
                pending = bool(git('rev-list', 'origin/main..HEAD'))
            except (RuntimeError, subprocess.TimeoutExpired):
                pending = False
            return self.send(200, {'html': content, 'baseHash': digest(content), 'site': SITE, 'pendingPush': pending})
        if path == '/api/status':
            return self.send(200, {'editor': 'yu-ohta', 'root': str(ROOT)})
        if path == '/index.html':
            return self.send(200, (ROOT / 'index.html').read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        return self.send(404, {'error': 'Not found.'})

    def do_POST(self):
        expected_origin = f'http://127.0.0.1:{self.server.server_port}'
        if not self.valid_host() or self.headers.get('Origin') != expected_origin or not secrets.compare_digest(self.headers.get('X-Editor-Token', ''), TOKEN):
            return self.send(403, {'error': 'この編集画面から操作してください。'})
        path = urlsplit(self.path).path
        if path not in ('/api/publish', '/api/push'):
            return self.send(404, {'error': 'Not found.'})
        if not LOCK.acquire(blocking=False):
            return self.send(409, {'error': '保存・公開処理中です。完了までお待ちください。'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > 6_000_000:
                return self.send(400, {'error': '入力サイズが不正です。'})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('入力形式が不正です。')
            if path == '/api/publish':
                result = save_and_publish(payload)
            else:
                # Retry only page-editor commits; do not silently publish unrelated work.
                verify_repo()
                pending = git('rev-list', 'origin/main..HEAD').splitlines()
                for commit in pending:
                    files = git('diff-tree', '--no-commit-id', '--name-only', '-r', commit).splitlines()
                    if files != ['index.html'] or git('show', '-s', '--format=%s', commit) != 'Update website content':
                        raise ValueError('別の作業のコミットが含まれています。ターミナルで確認してpushしてください。')
                if git('status', '--porcelain', '--', 'index.html'):
                    raise ValueError('未コミットの変更があります。保存して公開を再実行してください。')
                result = {'pushed': True, 'commit': push_changes(), 'site': SITE}
            self.send(200, result)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            self.send(409, {'error': str(error)})
        finally:
            LOCK.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    try:
        server = HTTPServer(('127.0.0.1', args.port), Handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        # Reuse our running editor so drafts keep the same browser origin.
        try:
            connection = HTTPConnection('127.0.0.1', args.port, timeout=2)
            connection.request('GET', '/api/status')
            status = json.loads(connection.getresponse().read())
            connection.close()
            if status != {'editor': 'yu-ohta', 'root': str(ROOT)}:
                raise ValueError('Different application.')
        except Exception:
            raise SystemExit(f'Port {args.port} is in use. Close that app, or choose --port NUMBER.')
        if not args.no_open:
            webbrowser.open(f'http://127.0.0.1:{args.port}/editor.html')
        return
    url = f'http://127.0.0.1:{server.server_port}/editor.html'
    print('Website editor: ' + url, flush=True)
    print('Stop: Ctrl+C', flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
