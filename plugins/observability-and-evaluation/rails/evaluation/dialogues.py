"""Persistent dialogue membership and atomic reconstruction from trace receipts."""
from hashlib import sha256
import json
import logging
from pathlib import Path
import time
from uuid import uuid4

from .merger import artifact_helpers, sequential_merger
from .state import locked, read_json, record_id, write_json

SESSION_POINTER = 'ojw_evaluation_dialogue'


def dialogue_id(session_id):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('A dialogue requires a nonempty SDK session ID')
    return sha256(('dialogue\0' + session_id).encode()).hexdigest()[:32]


def register_dialogue(root, session_id, *, launch_dir, name=None):
    root = Path(root).resolve()
    key = dialogue_id(session_id)
    directory = artifact_helpers().session_directory(root.parent, session_id)
    metadata_path = directory / 'metadata.json'
    context = {'id': key, 'kind': 'dialogue', 'name': name or 'Dialogue ' + session_id,
               'session_id': session_id, 'launch_dir': str(launch_dir),
               'metadata_path': str(metadata_path)}
    with locked(directory / '.dialogue.lock'):
        metadata = read_json(metadata_path)
        if metadata is None:
            metadata = {**context, 'service_root': str(root), 'artifact_layout_version': 2, 'exchange_count': 0,
                        'trajectory_path': str(directory / 'dialogue.json'),
                        'revision': 0, 'turn_count': 0, 'trace_count': 0, 'pending_turn_count': 0,
                        'status': 'pending', 'sources': [], 'updated_at': time.time(), 'ui_url': None}
            write_json(metadata_path, metadata)
        if metadata.get('session_id') != session_id or metadata.get('service_root') != str(root):
            raise ValueError('Dialogue metadata belongs to a different session or workspace')
        # Preserve the original launch directory when the host changes cwd.
        context['launch_dir'] = metadata['launch_dir']
        context['name'] = metadata['name']
        write_json(root / 'contexts' / f'{key}.json', context)
    return context


def begin_exchange(metadata_path, *, resuming=False):
    """Allocate one persisted number per user exchange, reusing it on resume."""
    path = Path(metadata_path)
    with locked(path.parent / '.dialogue.lock'):
        metadata = read_json(path)
        if metadata.get('artifact_layout_version') != 2:
            return None
        previous = metadata.get('exchange_count', 0)
        if type(previous) is not int or previous < 0:
            raise ValueError('Invalid saved exchange counter')
        if not resuming or previous == 0:
            previous += 1
            metadata['exchange_count'] = previous
            write_json(path, metadata)
        return previous


def read_dialogue(root, context_id):
    context = read_json(Path(root) / 'contexts' / f'{record_id(context_id)}.json')
    if not isinstance(context, dict) or context.get('kind') != 'dialogue':
        raise ValueError('Dialogue is unavailable; open its saved metadata or select another dialogue')
    value = read_json(context['metadata_path'])
    if not isinstance(value, dict) or value.get('id') != context_id:
        raise ValueError('Dialogue metadata is unavailable')
    return value


def public_dialogue(value):
    return {key: value.get(key) for key in ('id', 'name', 'session_id', 'revision', 'turn_count',
            'trace_count', 'pending_turn_count', 'status', 'error', 'updated_at')}


def list_dialogues(root):
    values = []
    for path in (Path(root) / 'contexts').glob('*.json'):
        context = read_json(path, {})
        if isinstance(context, dict) and context.get('kind') == 'dialogue':
            try:
                values.append(public_dialogue(read_dialogue(root, path.stem)))
            except (ValueError, TypeError, KeyError, OSError):
                continue
    return sorted(values, key=lambda item: item.get('updated_at') or 0, reverse=True)


def sync_dialogue(root, context_id, *, force=False):
    try:
        return _sync_dialogue(root, context_id, force=force)
    except Exception as exc:
        metadata = read_dialogue(root, context_id)
        with locked(Path(metadata['metadata_path']).parent / '.dialogue.lock'):
            metadata = read_dialogue(root, context_id)
            error = f'Cannot read dialogue records: {exc}'
            if metadata.get('status') != 'incomplete' or metadata.get('error') != error:
                metadata.update(status='incomplete', error=error, updated_at=time.time())
                write_json(metadata['metadata_path'], metadata)
        return metadata


def _sync_dialogue(root, context_id, *, force=False):
    root = Path(root).resolve()
    metadata = read_dialogue(root, context_id)
    target = Path(metadata['trajectory_path'])
    with locked(target.parent / '.dialogue.lock'):
        metadata = read_dialogue(root, context_id)
        previous = dict(metadata)
        sources, seen, turns, pending = [], set(), set(), 0
        fingerprint, problems = [], []
        receipts = []
        for path in (root / 'invocations').glob('*.json'):
            receipt = read_json(path)
            if not isinstance(receipt, dict) or receipt.get('session_id') != metadata['session_id']:
                continue
            context = (read_json(root / 'contexts' / f"{record_id(receipt['context_id'])}.json", {})
                       if receipt.get('context_id') else {})
            # Older workflow receipts did not include their kind. Their context
            # distinguishes a folder-derived label from an actual SDK session.
            if receipt.get('kind') != 'workflow' and context.get('kind') != 'workflow':
                receipts.append(receipt)
        receipts = sorted(receipts,
                          key=lambda r: (r.get('created_at', 0), r.get('id', '')))
        for receipt in receipts:
            paths = receipt.get('source_paths') or ([receipt['source_path']] if receipt.get('source_path') else [])
            record_id(receipt['id'])
            if not isinstance(paths, list) or any(not isinstance(p, str) or not p for p in paths):
                raise ValueError('A trace receipt must contain a list of nonempty file paths: ' + receipt['id'])
            pending += int(not paths or receipt.get('closed') is False)
            for raw_path in paths:
                path = Path(raw_path).resolve()
                if path in seen:
                    continue
                seen.add(path)
                try:
                    stat = path.stat()
                    if not path.is_file():
                        raise OSError('not a file')
                    fingerprint.append((str(path), stat.st_size, stat.st_mtime_ns))
                except OSError:
                    problems.append('A recorded trace is missing or unreadable: ' + str(path))
                    fingerprint.append((str(path), None, None))
                sources.append({'path': str(path), 'invocation_id': receipt['id']})
                exchange = receipt.get('exchange_index')
                turns.add(('exchange', exchange) if type(exchange) is int and exchange > 0 else ('invocation', receipt['id']))
        signature = sha256(json.dumps(fingerprint).encode()).hexdigest()
        metadata['pending_turn_count'] = pending
        launch = read_json(root / 'launch.json', {})
        if launch.get('url'):
            metadata['ui_url'] = launch['url'] + '&context=' + metadata['id']
        retry = metadata['status'] == 'incomplete' and (force or time.time() >= metadata.get('retry_at', 0))
        missing_target = sources and not target.is_file() and metadata['status'] != 'incomplete'
        if signature != metadata.get('source_signature') or missing_target or retry:
            metadata['source_signature'] = signature
            metadata['recorded_sources'] = sources
            if problems:
                metadata.update(status='incomplete', error='; '.join(problems))
            elif not sources:
                metadata.update(status='pending', error=None)
            else:
                temporary = target.with_name('.dialogue-' + uuid4().hex + '.json')
                try:
                    revision = metadata['revision'] + 1
                    sequential_merger()([s['path'] for s in sources], temporary, run_nr=revision,
                        scenario_meta={'uc': 'JiuwenSwarm', 'scenario': 'Dialogue',
                                       'system_architecture': 'shared_context',
                                       'system_architecture_description':
                                           'JiuwenSwarm chat dialogue: each exchange reads and '
                                           'extends one shared session history.'})
                    value = read_json(temporary)
                    value['trace_metadata']['run_id_path'] = str(target)
                    # The open tool's URL can appear in model/tool evidence.
                    # Keep its local bearer token out of the judge's trajectory.
                    token = read_json(root / 'access.json', {}).get('token')
                    serialized = json.dumps(value, ensure_ascii=False)
                    if token:
                        serialized = serialized.replace(token, '[evaluation-ui-token]')
                    write_json(temporary, json.loads(serialized))
                    digest = sha256(temporary.read_bytes()).hexdigest()
                    temporary.replace(target)
                    metadata.update(revision=revision, turn_count=len(turns), trace_count=len(sources),
                                    sources=sources, sha256=digest, status='ready', error=None)
                except Exception as exc:
                    # Keep the last good file, but never label it as current.
                    metadata.update(status='incomplete', error=f'Cannot merge this dialogue: {exc}')
                finally:
                    temporary.unlink(missing_ok=True)
            if metadata['status'] == 'incomplete':
                metadata['retry_at'] = time.time() + 5
            else:
                metadata.pop('retry_at', None)
        if metadata != previous:
            metadata['updated_at'] = time.time()
            write_json(metadata['metadata_path'], metadata)
        return metadata


def sync_dialogues(root):
    for context in list_dialogues(root):
        try:
            sync_dialogue(root, context['id'])
        except Exception as exc:
            # Even unreadable metadata or a write failure in one directory must
            # not prevent other dialogues from making progress.
            logging.getLogger(__name__).warning('Dialogue %s update failed (%s)', context['id'], type(exc).__name__)
