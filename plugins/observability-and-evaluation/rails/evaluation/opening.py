"""Explicitly reopen a recorded dialogue; never start an evaluation."""
import asyncio
from pathlib import Path

from openjiuwen.core.sys_operation.cwd import get_workspace

from .dialogues import SESSION_POINTER, dialogue_id, read_dialogue, register_dialogue, sync_dialogue
from .runtime import announce_service, ensure_service
from .state import read_json
from .dependencies import require_evaluation_dependencies


def open_saved_dialogue(path, *, port=8765):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / 'metadata.json'
    metadata = read_json(path)
    if not metadata or metadata.get('kind') != 'dialogue':
        raise ValueError('Select the dialogue directory or its metadata.json')
    root = Path(metadata['service_root']).resolve()
    context = register_dialogue(root, metadata['session_id'], launch_dir=metadata['launch_dir'], name=metadata['name'])
    if Path(context['metadata_path']) != path or context['id'] != metadata['id']:
        raise ValueError('Metadata does not match its recorded workspace and session')
    handle = ensure_service(root, workspace=root.parent, port=port)
    announce_service(handle)
    metadata = sync_dialogue(root, context['id'], force=True)
    return {'success': True, 'url': handle.url + '&context=' + context['id'],
            'context_id': context['id'], 'metadata_path': str(path),
            'revision': metadata['revision'], 'turn_count': metadata['turn_count'],
            'trace_count': metadata['trace_count'], 'status': metadata['status'],
            'message': 'Open this link to configure evaluation. Analysis starts only with Run Evaluation.'}


async def open_current_dialogue(session):
    try:
        if session is None:
            raise ValueError('The SDK did not provide a session; use the saved metadata.json to reopen the UI')
        require_evaluation_dependencies()
        sid = session.get_session_id()
        key = dialogue_id(sid)
        pointer = session.get_state(SESSION_POINTER)
        if pointer is not None:
            if not isinstance(pointer, dict) or pointer.get('session_id') != sid or pointer.get('context_id') != key:
                raise ValueError('Evaluation session reference does not match the current dialogue')
            workspace = pointer['workspace']
        else:
            workspace = get_workspace()
        if not workspace:
            raise ValueError('No evaluation workspace is available for this dialogue')
        metadata = read_dialogue(Path(workspace) / 'evaluation_ui', key)
        return await asyncio.to_thread(open_saved_dialogue, metadata['metadata_path'])
    except Exception as exc:
        return {'success': False, 'error': str(exc)}
