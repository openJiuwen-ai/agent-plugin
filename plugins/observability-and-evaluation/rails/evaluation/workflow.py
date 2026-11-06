"""Open evaluation setup for an explicitly supplied, completed trajectory."""
import asyncio
from hashlib import sha256
import logging
from pathlib import Path
import time
import webbrowser
from uuid import uuid4

from .runtime import ensure_service
from .state import write_json
from .dependencies import require_evaluation_dependencies

logger = logging.getLogger(__name__)


async def open_evaluation_ui(trajectory_path, *, workspace=None, name=None,
                             port=8765, open_browser=True):
    """Return a UI link pinned to this file; do not start an evaluation.

    Call after exporting the final workflow. ``workspace`` selects the shared
    UI service directory, defaulting to the input's parent. Evaluation outputs
    follow the file selected in the UI, including a manually selected file.
    The independent service survives the calling application's exit.
    """
    source = Path(trajectory_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError('trajectory_path must be a file')
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('port must be between 0 and 65535')
    if type(open_browser) is not bool:
        raise TypeError('open_browser must be a bool')
    require_evaluation_dependencies()
    workspace = Path(workspace).expanduser().resolve() if workspace is not None else source.parent
    root = workspace / 'evaluation_ui'
    key = sha256(('workflow\0' + str(source)).encode()).hexdigest()[:32]
    invocation = uuid4().hex
    context = {'id': key, 'kind': 'workflow', 'name': name or source.parent.name,
               'session_id': source.parent.name, 'launch_dir': str(source.parent),
               'latest_invocation': invocation}
    # The existing protocol supports pinned links and manual selection. No
    # directory scan or agent hook can reliably identify a scenario's final JSON.
    write_json(root / 'invocations' / f'{invocation}.json', {
        'id': invocation, 'context_id': key, 'kind': 'workflow', 'created_at': time.time(),
        'source_path': str(source), 'session_id': context['session_id']})
    write_json(root / 'contexts' / f'{key}.json', context)
    handle = await asyncio.to_thread(ensure_service, root, workspace=workspace, port=port)
    url = handle.url + '&context=' + key + '&invocation=' + invocation
    logger.warning('Evaluation UI ready: %s (connection details: %s)', url, root / 'launch.json')
    if open_browser:
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
        except Exception:
            opened = False
        if not opened:
            logger.warning('Browser unavailable. Open the Evaluation UI link above manually.')
    return url
