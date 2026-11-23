"""Local workspace evaluation service with durable jobs and invocation context."""
from __future__ import annotations

import argparse
import errno
import fcntl
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from openjiuwen.core.sys_operation.cwd import get_original_cwd

from .core.artifacts import EVALUATION_DIRECTORY_FORMAT, FAILURE_DETECTOR_RESULT, create_evaluation_directory
from .core.providers import PROVIDERS
from .core.config import TAXONOMIES, evaluation_defaults
from .domain.taxonomy import outline as taxonomy_outline
from .dependencies import EvaluationDependencyError, require_evaluation_dependencies
from .server_files import ServerFiles, ServerFileError
from .setup import MAX_BODY, check_output, trajectory_summary, validate_request
from .history import read_terminal_run, terminal_runs
from .domain.trajectories import step_ids, step_records
from .state import read_json, write_json, record_id
from .dialogues import list_dialogues, public_dialogue, read_dialogue, sync_dialogues

ASSETS = Path(__file__).resolve().parent / 'assets'


class EvaluationService:
    def __init__(self, output_dir, *, port=8765, session_id='standalone', worker_timeout=1800, browse_roots=None):
        require_evaluation_dependencies()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Capture once: browsing starts where this application was launched,
        # independently of the package location or later working-directory changes.
        roots = {'launch': Path(get_original_cwd()).resolve(), 'results': self.output_dir}
        roots.update(browse_roots or {})
        self.files = ServerFiles(roots)
        self.token = read_json(self.output_dir / 'access.json', {}).get('token') or secrets.token_urlsafe(32)
        write_json(self.output_dir / 'access.json', {'token': self.token})
        self.session_id = session_id
        self.worker_timeout = worker_timeout
        self.default_file = None
        self._default_receipt = None
        self._lock = threading.RLock()
        self._closed = False
        self.stop_event = threading.Event()
        self._processes = {}
        self._threads = {}
        self.jobs = {}
        self._terminal_records = {}
        for path in (self.output_dir / 'jobs').glob('*.json'):
            job = read_json(path)
            if not isinstance(job, dict) or job.get('id') != path.stem:
                continue
            if job.get('status') == 'running':
                result = (read_json(Path(job['output_dir']) / FAILURE_DETECTOR_RESULT)
                          or read_json(Path(job['output_dir']) / 'result.json'))
                job.update(result or {'status': 'interrupted', 'error': 'UI service stopped during evaluation. It was not automatically restarted.'})
                write_json(path, job)
            self.jobs[path.stem] = job
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, status, data, content_type='application/json', filename=None):
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                for name, value in {'Content-Type': content_type, 'Content-Length': str(len(body)),
                                    'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'}.items():
                    self.send_header(name, value)
                if filename:
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authenticated(self):
                return secrets.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + service.token).encode())

            def do_GET(self):
                url = urlsplit(self.path)
                path = url.path
                query = parse_qs(url.query, keep_blank_values=True)
                assets = {'/': ('html-demo_v4.html', 'text/html; charset=utf-8'),
                          '/evaluation_v4.js': ('evaluation_v4.js', 'text/javascript; charset=utf-8')}
                if path in assets:
                    name, mime = assets[path]
                    self.send(200, (ASSETS / name).read_bytes(), mime)
                    return
                if not self.authenticated():
                    self.send(403, {'error': 'Open the full evaluation URL, including its token'})
                    return
                try:
                    if path == '/api/health':
                        self.send(200, {'output_dir': str(service.output_dir), 'version': 2,
                                        'evaluation_directory_format': EVALUATION_DIRECTORY_FORMAT,
                                        'evaluation_directory_timezone': 'UTC',
                                        'evaluation_directory_suffix': 'ui',
                                        'evaluation_result_filename': FAILURE_DETECTOR_RESULT})
                    elif path == '/api/config':
                        self.send(200, service.config(query.get('context', [None])[0], query.get('invocation', [None])[0]))
                    elif path == '/api/dialogues':
                        self.send(200, {'dialogues': list_dialogues(service.output_dir)})
                    elif path == '/api/files':
                        self.send(200, service.files.list_directory(query.get('root', [''])[0], query.get('path', [''])[0], int(query.get('offset', ['0'])[0])))
                    elif path == '/api/runs':
                        context_id = query.get('context', [None])[0]
                        jobs = service.history(context_id)
                        self.send(200, {'runs': sorted(jobs, key=lambda j: j.get('created_at', 0), reverse=True)})
                    elif match := re.fullmatch(r'/api/runs/([0-9a-f]{32})(/annotations|/trajectory)?', path):
                        job = service.history_job(match[1])
                        if not job:
                            self.send(404, {'error': 'Evaluation not found'})
                        elif match[2] == '/trajectory':
                            snapshot = read_json(Path(job['output_dir']) / 'trajectory.json')
                            if snapshot is None:
                                self.send(404, {'error': 'Saved evaluation trajectory is unavailable'})
                            else:
                                self.send(200, {'trajectory_json': json.dumps(snapshot, ensure_ascii=False, indent=2),
                                                'steps': step_records(snapshot), 'evaluation_id': job['id']})
                        elif not match[2]:
                            # Read the immutable evaluation snapshot, never the current input.
                            snapshot = read_json(Path(job['output_dir']) / 'trajectory.json')
                            job['trajectory_step_ids'] = step_ids(snapshot) if snapshot is not None else None
                            self.send(200, job)
                        else:
                            artifact = Path(job.get('annotations_file') or '').resolve()
                            if job['status'] not in ('completed', 'partial') or not artifact.is_relative_to(Path(job['output_dir']).resolve()) or not artifact.is_file():
                                self.send(404, {'error': 'Generated annotations are unavailable'})
                            else:
                                self.send(200, artifact.read_bytes(), filename='annotations.json')
                    else:
                        self.send(404, {'error': 'Not found'})
                except (ValueError, ServerFileError) as exc:
                    self.send(400, {'error': str(exc)})

            def do_POST(self):
                if not self.authenticated():
                    self.send(403, {'error': 'Invalid session token'})
                    return
                origin = self.headers.get('Origin')
                if origin and origin != 'http://' + self.headers.get('Host', ''):
                    self.send(403, {'error': 'Cross-origin requests are not allowed'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= MAX_BODY:
                        self.send(413, {'error': 'Upload limit is 10 MiB'})
                        return
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        self.send(415, {'error': 'Use application/json'})
                        return
                    self.connection.settimeout(20)
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError('Expected a JSON object')
                    if self.path == '/api/shutdown':
                        self.send(200, {'status': 'stopping'})
                        service.stop_event.set()
                        return
                    if self.path not in ('/api/inspect', '/api/trajectory', '/api/runs'):
                        self.send(404, {'error': 'Not found'})
                        return
                    request_id = data.pop('request_id', None)
                    if request_id is not None:
                        record_id(request_id)
                    context_id = data.pop('context_id', None)
                    context = service.context(context_id)
                    with service._lock:
                        existing = dict(service.jobs.get(request_id, {}))
                    if self.path == '/api/runs' and existing:
                        if existing.get('context_id') != context_id:
                            raise ValueError('Evaluation ID belongs to another agent context')
                        self.send(202, existing)
                        return
                    invocation = data.pop('invocation_id', None)
                    if invocation:
                        service.invocation(invocation, context_id)
                    expected = data.pop('expected_sha256', None)
                    name = data.pop('upload_name', 'trajectory.json')
                    trajectory, source, digest = service.read_input(data)
                    dialogue = service.dialogue_snapshot(context, source, digest)
                    summary = trajectory_summary(trajectory)
                    if self.path == '/api/trajectory':
                        if expected and expected != digest:
                            raise ValueError('The selected file changed. Refresh the trajectory selection and try again.')
                        # Preview only: no result directory, job, or provider call.
                        self.send(200, {'trajectory_json': json.dumps(trajectory, ensure_ascii=False, indent=2),
                                        'sha256': digest, 'steps': step_records(trajectory)})
                        return
                    base = source.parent if source is not None else service.upload_root(context_id)
                    output = check_output(base / 'evaluation')
                    info = dict(summary, source_path=str(source) if source else None, source_kind='server' if source else 'upload',
                                name=source.name if source else Path(str(name)).name[:255], sha256=digest, output_parent=str(output),
                                context=context, invocation_id=invocation, dialogue=dialogue)
                    if self.path == '/api/inspect':
                        self.send(200, info)
                        return
                    if expected and expected != digest:
                        raise ValueError('The selected file changed. Select it again to review the new contents before running.')
                    request = validate_request(data)
                    self.send(202, service.submit(request, source=source, source_digest=digest, input_info=info, context_id=context_id, request_id=request_id))
                except (json.JSONDecodeError, UnicodeError):
                    self.send(400, {'error': 'The request does not contain valid UTF-8 JSON'})
                except (ValueError, ServerFileError) as exc:
                    self.send(400, {'error': str(exc)})
                except RuntimeError as exc:
                    self.send(409, {'error': str(exc)})
                except OSError:
                    self.send(400, {'error': 'Cannot read the input or write evaluation state. Check folder permissions.'})

        self.httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.port = self.httpd.server_port
        self.url = f'http://127.0.0.1:{self.port}/#v=setup&token={self.token}'
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self._sync_stop = threading.Event()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()

    def _sync_loop(self):
        while not self._sync_stop.is_set():
            try:
                sync_dialogues(self.output_dir)
            except Exception as exc:
                logging.getLogger(__name__).warning('Dialogue update failed (%s)', type(exc).__name__)
            self._sync_stop.wait(.25)

    def history(self, context_id):
        """Combine UI jobs with terminal records for this context's inputs."""
        with self._lock:
            context = self.context(context_id)
            jobs = [dict(j) for j in self.jobs.values() if j.get('context_id') == context_id]
            sources = {j.get('input', {}).get('source_path') for j in jobs}
            if context:
                # Include pinned/earlier workflow invocations, not other contexts.
                for path in (self.output_dir / 'invocations').glob('*.json'):
                    receipt = read_json(path, {})
                    if isinstance(receipt, dict) and receipt.get('context_id') == context_id:
                        sources.add(receipt.get('source_path'))
                if context.get('kind') == 'dialogue':
                    metadata = read_dialogue(self.output_dir, context_id)
                    sources.add(metadata.get('trajectory_path'))
            else:
                self.refresh_default_file()
                if self.default_file:
                    sources.add(str(self.files.resolve(self.default_file['root'], self.default_file['path'])))
            # Refresh records from disk without importing them into UI-managed jobs.
            self._terminal_records = {key: value for key, value in self._terminal_records.items()
                                      if value[2] != context_id}
            seen = set(self.jobs)
            for source in sorted(s for s in sources if isinstance(s, str) and s):
                for job in terminal_runs(source):
                    if job['id'] in seen:
                        continue
                    seen.add(job['id'])
                    self._terminal_records[job['id']] = (job['output_dir'], source, context_id)
                    jobs.append({**job, 'context_id': context_id})
            return jobs

    def history_job(self, job_id):
        with self._lock:
            if job_id in self.jobs:
                return dict(self.jobs[job_id])
            record = self._terminal_records.get(job_id)
            if record:
                directory, source, context_id = record
                job = read_terminal_run(directory, source)
                if job and job['id'] == job_id:
                    return {**job, 'context_id': context_id}
            return {}

    def context(self, context_id):
        if context_id is None:
            return None
        value = read_json(self.output_dir / 'contexts' / f'{record_id(context_id)}.json')
        if not value:
            raise ValueError('Evaluation context is unavailable. Open its latest evaluation link.')
        return value

    def invocation(self, invocation_id, context_id, *, allow_missing=False):
        value = read_json(self.output_dir / 'invocations' / f'{record_id(invocation_id)}.json')
        if value is None and allow_missing:
            return None
        if not value or value.get('context_id') != context_id:
            raise ValueError('Invocation does not belong to this evaluation context')
        return value

    def selection(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            return None
        root = 'trace_' + sha256(str(path.parent).encode()).hexdigest()[:16]
        self.files.roots[root] = path.parent
        return {'root': root, 'path': path.name}

    def default_directory(self, context, selection):
        if selection:
            parent = Path(selection['path']).parent.as_posix()
            return {'root': selection['root'], 'path': '' if parent == '.' else parent}
        launch = self.files.roots['launch']
        directory = Path(context.get('launch_dir') or launch).resolve() if context else launch
        if not directory.is_dir():
            directory = launch
        root = 'launch' if directory == launch else 'launch_' + sha256(str(directory).encode()).hexdigest()[:16]
        self.files.roots[root] = directory
        return {'root': root, 'path': ''}

    def config(self, context_id=None, invocation_id=None):
        with self._lock:
            context = self.context(context_id)
            # Trace handoff may be incomplete; setup must still offer manual input.
            # Existing receipts must always belong to the requested context.
            current = self.invocation(context['latest_invocation'], context_id, allow_missing=True) if context and context.get('latest_invocation') else None
            pinned = self.invocation(invocation_id, context_id, allow_missing=True) if invocation_id else current
            default = self.selection(pinned['source_path']) if pinned and pinned.get('source_path') else None
            latest = self.selection(current['source_path']) if current and current.get('source_path') else None
            dialogue = None
            if context and context.get('kind') == 'dialogue':
                metadata = read_dialogue(self.output_dir, context_id)
                dialogue = public_dialogue(metadata)
                latest = self.selection(metadata['trajectory_path']) if metadata['status'] == 'ready' else None
                if not invocation_id:
                    default = latest
            if not context_id:
                self.refresh_default_file()
                default = self.default_file
                if default and not self.files.resolve(default['root'], default['path']).is_file():
                    default = None
            directory = self.default_directory(context, default)
            latest_signature = None
            if latest:
                try:
                    stat = self.files.resolve(latest['root'], latest['path']).stat()
                    # Detect edits/replacement without repeatedly parsing large
                    # trajectories. /api/inspect still validates JSON and digest.
                    latest_signature = f'{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'
                except (OSError, ValueError):
                    if default == latest:
                        default = None
                    latest = None
            defaults = evaluation_defaults()
            defaults.pop('output_subdir', None)
            return {'providers': {k: {'model': v['default_model'], 'base_url': v['base_url']} for k, v in PROVIDERS.items()},
                    'defaults': defaults, 'file_roots': self.files.describe(), 'context': context, 'invocation': pinned,
                    'latest_invocation': current, 'latest_file': latest, 'latest_file_signature': latest_signature,
                    'default_file': default,
                    # Full hierarchy with definitions, so the UI can explain each finding.
                    'taxonomies': {key: taxonomy_outline(path) for key, path in TAXONOMIES.items()},
                    'default_directory': directory, 'dialogue': dialogue,
                    'dialogues': list_dialogues(self.output_dir),
                    'session_id': context['session_id'] if context else self.session_id,
                    'output_dir': str(self.upload_root(context_id))}

    def upload_root(self, context_id):
        return self.output_dir / 'uploads' / record_id(context_id) if context_id else self.output_dir

    def read_input(self, data):
        source = None
        if 'server_file' in data:
            if 'trajectory' in data:
                raise ValueError('Choose upload or server file, not both')
            selection = data.pop('server_file')
            trajectory, digest = self.files.read_json(selection, max_bytes=MAX_BODY, with_digest=True)
            source = self.files.resolve(selection['root'], selection['path'])
            data['trajectory'] = trajectory
        else:
            trajectory = data.get('trajectory')
            digest = sha256(json.dumps(trajectory, ensure_ascii=False).encode()).hexdigest()
        return trajectory, source, digest

    def dialogue_snapshot(self, context, source, digest):
        if not context or context.get('kind') != 'dialogue' or source is None:
            return None
        metadata = read_dialogue(self.output_dir, context['id'])
        if source != Path(metadata['trajectory_path']):
            return None
        if metadata['status'] != 'ready':
            raise ValueError(metadata.get('error') or 'The dialogue trajectory is not ready')
        if digest != metadata.get('sha256'):
            raise ValueError('The dialogue changed during preview. Refresh the trajectory and try again.')
        return public_dialogue(metadata)

    def set_default_receipt(self, path):
        self._default_receipt = Path(path)
        self.default_file = None

    def refresh_default_file(self):
        receipt = read_json(self._default_receipt) if self._default_receipt else None
        if receipt and receipt.get('source_path'):
            self.set_default_file(receipt['source_path'])

    def set_default_file(self, path):
        with self._lock:
            self.default_file = self.selection(path)

    def _save_job(self, job):
        write_json(self.output_dir / 'jobs' / f'{job["id"]}.json', job)
        write_json(Path(job['output_dir']) / 'evaluation.json', job)

    def submit(self, request, *, source=None, source_digest=None, input_info=None, context_id=None, request_id=None):
        with self._lock:
            if self._closed:
                raise RuntimeError('Evaluation UI has stopped')
            if request_id in self.jobs:
                return dict(self.jobs[request_id])
            if any(j['status'] == 'running' and j.get('context_id') == context_id for j in self.jobs.values()):
                raise RuntimeError('An evaluation is already running for this agent. Open it in Recent evaluations.')
            job_id = record_id(request_id) if request_id else uuid4().hex
            base = source.parent if source is not None else self.upload_root(context_id)
            job_dir = create_evaluation_directory(check_output(base / 'evaluation'), 'ui')
            trajectory = request.pop('trajectory')
            snapshot = json.dumps(trajectory, ensure_ascii=False).encode('utf-8')
            (job_dir / 'trajectory.json').write_bytes(snapshot)
            write_json(job_dir / 'source.json', {'source_path': str(source) if source else None,
                'source_kind': 'server' if source else 'upload', 'source_sha256': source_digest,
                'dialogue': (input_info or {}).get('dialogue'),
                'snapshot': 'trajectory.json', 'snapshot_sha256': sha256(snapshot).hexdigest()})
            parameters = {k: v for k, v in request.items() if k != 'api_key'}
            job = {'id': job_id, 'status': 'running', 'created_at': time.time(), 'context_id': context_id,
                   'output_dir': str(job_dir), 'input': input_info or {'source_path': str(source) if source else None},
                   'parameters': parameters}
            self._save_job(job)
            self.jobs[job_id] = job
            thread = threading.Thread(target=self._run, args=(job_id, dict(request), job_dir), daemon=True)
            self._threads[job_id] = thread
            thread.start()
            return dict(job)

    def _run(self, job_id, request, job_dir):
        result = {'status': 'failed', 'error': 'Evaluation worker could not start. Check the installed evaluation dependencies.'}
        try:
            request['job_dir'] = str(job_dir)
            with self._lock:
                if self._closed:
                    return
                process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('worker.py')), str(os.getpid())],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
                self._processes[job_id] = process
            try:
                process.communicate(json.dumps(request), timeout=self.worker_timeout)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate()
                result = {'status': 'failed', 'error': 'Evaluation timed out'}
            else:
                result = (read_json(job_dir / FAILURE_DETECTOR_RESULT)
                          or read_json(job_dir / 'result.json', result))
                if process.returncode and result.get('status') == 'completed':
                    result = {'status': 'failed', 'error': 'Evaluation worker exited unexpectedly'}
        except OSError:
            result = {'status': 'failed', 'error': 'Cannot start the evaluation worker or write its artifacts'}
        finally:
            request.clear()
            with self._lock:
                self._processes.pop(job_id, None)
                if self._closed:
                    result = {'status': 'cancelled', 'error': 'Evaluation service was explicitly stopped'}
                self.jobs[job_id].update(result, finished_at=time.time())
                try:
                    self._save_job(self.jobs[job_id])
                except OSError:
                    self.jobs[job_id]['error'] = 'Could not persist evaluation status. Check disk space and permissions.'

    def close(self):
        self._sync_stop.set()
        self._sync_thread.join(timeout=5)
        with self._lock:
            self._closed = True
            for process in self._processes.values():
                if process.poll() is None:
                    process.kill()
        self.httpd.shutdown(); self.httpd.server_close()
        self._thread.join(timeout=3)
        for thread in list(self._threads.values()):
            thread.join(timeout=5)


def main():
    from .runtime import running_service
    parser = argparse.ArgumentParser(description='Serve the local evaluation UI independently of agent lifetime')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--workspace', help='Additional directory available in the server file picker')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--stop', action='store_true', help='Stop this workspace UI and cancel its running evaluations')
    action.add_argument('--status', action='store_true', help='Print the running workspace UI URL')
    args = parser.parse_args()
    root = Path(args.output_dir).expanduser().resolve()
    existing = running_service(root)
    if args.stop or args.status:
        if not existing:
            print('Evaluation UI is not running', flush=True)
        elif args.stop:
            existing.api('/api/shutdown', {})
            print('Evaluation UI stopping', flush=True)
        else:
            print(f'Evaluation UI: {existing.url}', flush=True)
        return
    try:
        require_evaluation_dependencies()
    except EvaluationDependencyError as exc:
        raise SystemExit(str(exc)) from None
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if existing:
                print(f'Evaluation UI: {existing.url}', flush=True)
                return
            raise RuntimeError('An evaluation service is already starting in this folder') from None
        roots = {'workspace': args.workspace} if args.workspace else None
        previous = read_json(root / 'launch.json', {})
        port = previous.get('port', args.port) if args.port == 8765 else args.port
        try:
            service = EvaluationService(root, port=port, browse_roots=roots)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            service = EvaluationService(root, port=0, browse_roots=roots)
        write_json(root / 'launch.json', {'url': service.url, 'token': service.token, 'port': service.port})
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: service.stop_event.set())
        print(f'Evaluation UI: {service.url}', flush=True)
        try:
            service.stop_event.wait()
        finally:
            service.close()


if __name__ == '__main__':
    main()
