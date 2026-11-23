"""Safe dialogue paths and exclusive, readable artifact names."""
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re


def session_directory(workspace, session_id):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('A dialogue requires a nonempty session ID')
    base = Path(workspace).resolve() / 'observability_traces'
    base.mkdir(parents=True, exist_ok=True)
    digest = sha256(session_id.encode()).hexdigest()
    legacy = base / digest[:16]

    def owner(directory):
        if directory.is_symlink() or directory.resolve().parent != base.resolve():
            raise ValueError('Dialogue directory must remain inside observability_traces')
        marker = directory / '.session-id'
        if marker.exists():
            if marker.is_symlink():
                raise ValueError('Dialogue ownership marker must be a regular file')
            return marker.read_text(encoding='utf-8')
        metadata = directory / 'metadata.json'
        if metadata.is_file():
            return json.loads(metadata.read_text(encoding='utf-8'))['session_id']
        return None

    # Both Rails claim directories through the same lock. The ownership marker
    # also protects sessions recorded by the observer without EvaluationRail.
    with (base / '.sessions.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if legacy.is_dir() and owner(legacy) in (None, session_id):
            # Existing recordings retain their paths, including incomplete ones.
            return legacy
        component = (session_id if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}', session_id)
                     else 'session_' + digest)
        attempt = 0
        while True:
            directory = base / component
            existing_owner = owner(directory)
            if existing_owner == session_id:
                return directory
            if not directory.exists() or (directory.is_dir() and not any(directory.iterdir())):
                directory.mkdir(exist_ok=True)
                marker = directory / '.session-id'
                with marker.open('x', encoding='utf-8') as stream:
                    stream.write(session_id)
                return directory
            attempt += 1
            component = 'session_' + sha256(f'dialogue-path\0{attempt}\0{session_id}'.encode()).hexdigest()


def reserve_artifact(directory, agent_name, naming):
    """Reserve a JSON name before writing; completed evidence is never replaced."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[^A-Za-z0-9_-]', '_', agent_name) or 'agent'
    kind = naming.get('kind')
    index = naming.get('exchange_index')
    exchange = kind == 'exchange' and type(index) is int and index > 0
    if exchange:
        base = f'agent_{name}_ctx_{index}'
    elif kind == 'command':
        base = 'command_evaluate'
    else:
        base = f'unlinked_agent_{name}'
    pattern = re.compile(re.escape(base) + (r'(?:_part_(\d+))?\.json' if exchange else r'_(\d+)\.json'))
    with (directory / '.artifacts.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        numbers = []
        for path in directory.glob(base + '*.json'):
            match = pattern.fullmatch(path.name)
            if match:
                numbers.append(int(match.group(1) or 1))
        part = max(numbers, default=0) + 1
        while True:
            suffix = ('' if part == 1 else f'_part_{part}') if exchange else f'_{part}'
            path = directory / (base + suffix + '.json')
            if any(path.with_suffix(extension).exists() for extension in ('.transcript.jsonl', '.events.jsonl')):
                part += 1
                continue
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                part += 1
                continue
            os.close(fd)
            return path
