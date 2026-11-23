"""Authenticated file browsing is limited to explicitly supplied directories."""
import json
from hashlib import sha256
from pathlib import Path

PAGE_SIZE = 200


class ServerFileError(ValueError):
    pass


class ServerFiles:
    def __init__(self, roots):
        self.roots = {name: Path(path).expanduser().resolve() for name, path in roots.items()}

    def describe(self):
        labels = {'project': 'Project', 'workspace': 'Workspace', 'results': 'Results', 'launch': 'Launch directory'}
        return [{'id': name, 'label': 'Launch directory' if name.startswith('launch_') else labels.get(name, name), 'path': str(path)}
                for name, path in self.roots.items()]

    def resolve(self, root_id, relative):
        if not isinstance(root_id, str) or root_id not in self.roots:
            raise ServerFileError('Select an available server folder')
        if not isinstance(relative, str) or '\x00' in relative:
            raise ServerFileError('Invalid server path')
        path = Path(relative)
        if path.is_absolute() or any(part == '..' or part.startswith('.') for part in path.parts):
            raise ServerFileError('Choose a file inside the available server folders')
        root = self.roots[root_id]
        current = root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ServerFileError('Symbolic links are not available in the file picker')
        resolved = current.resolve()
        if not resolved.is_relative_to(root):
            raise ServerFileError('Choose a file inside the available server folders')
        return resolved

    def list_directory(self, root_id, relative='', offset=0):
        if type(offset) is not int or offset < 0:
            raise ServerFileError('Invalid page')
        directory = self.resolve(root_id, relative)
        if not directory.is_dir():
            raise ServerFileError('Server folder no longer exists or is unavailable')
        entries = []
        try:
            for path in directory.iterdir():
                if path.name.startswith('.') or path.name in {'__pycache__', 'access.json', 'launch.json'} or path.is_symlink():
                    continue
                try:
                    is_dir = path.is_dir()
                    if is_dir or (path.is_file() and path.suffix.lower() == '.json'):
                        entries.append({'name': path.name, 'kind': 'directory' if is_dir else 'file',
                                        'path': path.relative_to(self.roots[root_id]).as_posix(),
                                        'size': None if is_dir else path.stat().st_size})
                except OSError:
                    continue
        except OSError:
            raise ServerFileError('Cannot read this server folder') from None
        entries.sort(key=lambda x: (x['kind'] != 'directory', x['name'].casefold()))
        relative = directory.relative_to(self.roots[root_id]).as_posix()
        return {'root': root_id, 'path': '' if relative == '.' else relative,
                'entries': entries[offset:offset + PAGE_SIZE],
                'next_offset': offset + PAGE_SIZE if offset + PAGE_SIZE < len(entries) else None}

    def read_json(self, selection, *, max_bytes, with_digest=False):
        if not isinstance(selection, dict) or set(selection) != {'root', 'path'}:
            raise ServerFileError('Select one JSON file from the server')
        path = self.resolve(selection['root'], selection['path'])
        if path.suffix.lower() != '.json' or not path.is_file():
            raise ServerFileError('Selected JSON file no longer exists or is unavailable')
        try:
            with path.open('rb') as stream:
                data = stream.read(max_bytes + 1)
        except OSError:
            raise ServerFileError('Cannot read the selected JSON file') from None
        if len(data) > max_bytes:
            raise ServerFileError('Selected JSON exceeds the 10 MiB limit')
        try:
            value = json.loads(data)
            return (value, sha256(data).hexdigest()) if with_digest else value
        except (ValueError, UnicodeError):
            raise ServerFileError('Selected file does not contain valid JSON') from None
